#!/usr/bin/env python3
"""
dnstt-balancer: Multi-tunnel SOCKS5 load balancer for dnstt-client.

Spawns multiple dnstt-client processes (one per DNS resolver), exposes a
unified SOCKS5 proxy, and distributes connections across healthy tunnels
with latency-weighted routing, health monitoring, auto-retry on failure,
and a live terminal dashboard.

Usage:
    python3 dnstt-balancer.py \\
        --dns-list working_dns_servers.txt \\
        --pubkey <pub key> \\
        --domain <domain>

    Then point browser/Telegram SOCKS5 proxy to 127.0.0.1:8080
"""

import argparse
import asyncio
import logging
import os
import platform
import random
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set, Tuple

IS_WINDOWS = platform.system() == "Windows"

if not IS_WINDOWS:
    import resource

# ─── Constants ────────────────────────────────────────────────────────────────

SOCKS5_VER = 0x05
SOCKS5_AUTH_NONE = 0x00
SOCKS5_CMD_CONNECT = 0x01
SOCKS5_CMD_BIND = 0x02
SOCKS5_CMD_UDP_ASSOC = 0x03

SOCKS5_ATYP_IPV4 = 0x01
SOCKS5_ATYP_DOMAIN = 0x03
SOCKS5_ATYP_IPV6 = 0x04

SOCKS5_REP_SUCCESS = 0x00
SOCKS5_REP_GENERAL = 0x01
SOCKS5_REP_NOT_ALLOWED = 0x02
SOCKS5_REP_NET_UNREACH = 0x03
SOCKS5_REP_HOST_UNREACH = 0x04
SOCKS5_REP_REFUSED = 0x05
SOCKS5_REP_CMD_NOT_SUPPORTED = 0x07

BASE_PORT = 30000
RELAY_BUF = 65536  # 64KB relay buffer
HEALTH_TARGET_HOST = "www.gstatic.com"
HEALTH_TARGET_PORT = 443
MAX_CONSECUTIVE_FAILURES = 3
MAX_RETRIES = 2  # retry on different tunnel if upstream connect fails
NO_TUNNEL_WAIT = 3.0  # seconds to wait if no healthy tunnel, before giving up
NO_TUNNEL_POLL = 0.3  # poll interval while waiting for a tunnel

logger = logging.getLogger("dnstt-balancer")


# ─── Ring-buffer log handler for dashboard ────────────────────────────────────


class RingBufferHandler(logging.Handler):
    """Stores recent log records in a deque for dashboard display."""

    def __init__(self, capacity: int = 100):
        super().__init__()
        self.records: Deque[str] = deque(maxlen=capacity)

    def emit(self, record):
        try:
            msg = self.format(record)
            self.records.append(msg)
        except Exception:
            self.handleError(record)

    def get_recent(self, n: int = 8) -> List[str]:
        return list(self.records)[-n:]


# ─── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class TunnelStats:
    bytes_tx: int = 0
    bytes_rx: int = 0
    total_connections: int = 0
    active_connections: int = 0
    failed_connections: int = 0


@dataclass
class DnsttTunnel:
    tunnel_id: int
    dns_server: str
    socks_port: int
    process: Optional[subprocess.Popen] = None
    healthy: bool = False
    latency: float = float("inf")
    stats: TunnelStats = field(default_factory=TunnelStats)
    started_at: float = 0.0
    last_health_check: float = 0.0
    consecutive_failures: int = 0
    stderr_path: str = ""

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


# ─── Tunnel Pool ──────────────────────────────────────────────────────────────


class TunnelPool:
    """Manages spawning and lifecycle of dnstt-client tunnel processes."""

    def __init__(
        self,
        dnstt_path: str,
        resolvers: List[str],
        pubkey: str,
        domain: str,
        dns_port: int = 53,
        protocol: str = "udp",
        utls: Optional[str] = None,
        max_tunnels: int = 15,
        startup_wait: float = 6.0,
    ):
        self.dnstt_path = os.path.abspath(dnstt_path)
        self.pubkey = pubkey
        self.domain = domain
        self.dns_port = dns_port
        self.protocol = protocol
        self.utls = utls
        self.max_tunnels = max_tunnels
        self.startup_wait = startup_wait

        # Shuffle so we don't always pick the same ones
        random.shuffle(resolvers)
        n = min(max_tunnels, len(resolvers))
        self.active_resolvers = resolvers[:n]
        self.reserve_resolvers: Deque[str] = deque(resolvers[n:])
        self.dead_resolvers: Set[str] = set()

        self.tunnels: Dict[int, DnsttTunnel] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_dnstt_name() -> str:
        """Return a sensible default binary name for the current platform."""
        s = platform.system()
        m = platform.machine().lower()
        if s == "Windows":
            return ".\\dnstt-client-windows-amd64.exe"
        elif s == "Darwin":
            arch = "arm64" if m in ("arm64", "aarch64") else "amd64"
            return f"./dnstt-client-darwin-{arch}"
        else:
            arch = "arm64" if m in ("arm64", "aarch64") else "amd64"
            return f"./dnstt-client-linux-{arch}"

    def _build_cmd(self, dns_server: str, socks_port: int) -> List[str]:
        dns_target = f"{dns_server}:{self.dns_port}"
        cmd = [self.dnstt_path, f"-{self.protocol}", dns_target]
        if self.utls:
            cmd += ["-utls", self.utls]
        cmd += ["-pubkey", self.pubkey, self.domain, f"127.0.0.1:{socks_port}"]
        return cmd

    def _wait_for_port(self, port: int, timeout: float) -> bool:
        """Block until 127.0.0.1:port is accepting TCP, or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1.0)
                result = s.connect_ex(("127.0.0.1", port))
                s.close()
                if result == 0:
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    async def _spawn_tunnel(
        self, tunnel_id: int, dns_server: str
    ) -> Optional[DnsttTunnel]:
        """Spawn a single dnstt-client process and wait for its SOCKS port."""
        socks_port = BASE_PORT + tunnel_id
        stderr_path = os.path.join(
            tempfile.gettempdir(), f"dnstt_balancer_{tunnel_id}.log"
        )
        cmd = self._build_cmd(dns_server, socks_port)

        try:
            stderr_file = open(stderr_path, "w+b")
            popen_kwargs: dict = dict(
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )
            if IS_WINDOWS:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["preexec_fn"] = os.setsid
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as e:
            logger.error(f"Failed to spawn tunnel #{tunnel_id} via {dns_server}: {e}")
            return None

        # Wait for SOCKS port in a thread (blocking I/O)
        loop = asyncio.get_event_loop()
        ready = await loop.run_in_executor(
            None, self._wait_for_port, socks_port, self.startup_wait + 3
        )

        if proc.poll() is not None:
            logger.warning(f"Tunnel #{tunnel_id} ({dns_server}) died during startup")
            try:
                stderr_file.seek(0)
                err = stderr_file.read(500).decode("utf-8", errors="replace")
                if err.strip():
                    logger.warning(f"  stderr: {err.strip()[:200]}")
            except Exception:
                pass
            stderr_file.close()
            return None

        if not ready:
            logger.warning(
                f"Tunnel #{tunnel_id} ({dns_server}) SOCKS port not ready, killing"
            )
            self._kill_process(proc)
            stderr_file.close()
            return None

        tunnel = DnsttTunnel(
            tunnel_id=tunnel_id,
            dns_server=dns_server,
            socks_port=socks_port,
            process=proc,
            healthy=True,
            started_at=time.time(),
            stderr_path=stderr_path,
        )
        logger.info(f"Tunnel #{tunnel_id} started: {dns_server} -> :{socks_port}")
        return tunnel

    def _kill_process(self, proc: subprocess.Popen):
        """Terminate a process tree, escalating as needed."""
        if IS_WINDOWS:
            # On Windows, kill the entire process tree with taskkill /T
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    timeout=5,
                    capture_output=True,
                )
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

    async def spawn_all(self):
        """Spawn all initial tunnels concurrently."""
        tasks = []
        for dns_server in self.active_resolvers:
            tid = self._next_id
            self._next_id += 1
            tasks.append(self._spawn_tunnel(tid, dns_server))

        results = await asyncio.gather(*tasks)

        for tunnel in results:
            if tunnel is not None:
                self.tunnels[tunnel.tunnel_id] = tunnel

        healthy = sum(1 for t in self.tunnels.values() if t.healthy)
        logger.info(
            f"Spawned {len(self.tunnels)}/{len(self.active_resolvers)} tunnels "
            f"({healthy} healthy)"
        )

        # If we didn't reach max_tunnels, immediately try reserves
        if len(self.tunnels) < self.max_tunnels and self.reserve_resolvers:
            logger.info(
                f"Only {len(self.tunnels)}/{self.max_tunnels} tunnels up, "
                f"trying {len(self.reserve_resolvers)} reserves..."
            )
            await self.fill_up()

    def get_healthy_tunnels(self) -> List[DnsttTunnel]:
        return [t for t in self.tunnels.values() if t.healthy and t.is_alive()]

    def get_best_tunnel(self) -> Optional[DnsttTunnel]:
        """Select a tunnel using latency-weighted random routing."""
        healthy = self.get_healthy_tunnels()
        if not healthy:
            return None

        weights = []
        for t in healthy:
            lat = t.latency if t.latency < float("inf") else 5.0
            w = 1.0 / (max(lat, 0.01) * (1 + t.stats.active_connections))
            weights.append(w)

        return random.choices(healthy, weights=weights, k=1)[0]

    def get_alt_tunnel(self, exclude: Set[int]) -> Optional[DnsttTunnel]:
        """Get a healthy tunnel excluding given IDs (for retry)."""
        candidates = [
            t
            for t in self.tunnels.values()
            if t.healthy and t.is_alive() and t.tunnel_id not in exclude
        ]
        if not candidates:
            return None

        weights = []
        for t in candidates:
            lat = t.latency if t.latency < float("inf") else 5.0
            w = 1.0 / (max(lat, 0.01) * (1 + t.stats.active_connections))
            weights.append(w)

        return random.choices(candidates, weights=weights, k=1)[0]

    async def replace_tunnel(self, tunnel: DnsttTunnel):
        """Kill a dead tunnel and spawn a replacement from the reserve pool."""
        async with self._lock:
            # Guard: already removed by another coroutine
            if tunnel.tunnel_id not in self.tunnels:
                return

            logger.warning(
                f"Replacing tunnel #{tunnel.tunnel_id} ({tunnel.dns_server})"
            )

            if tunnel.process and tunnel.is_alive():
                self._kill_process(tunnel.process)

            self.dead_resolvers.add(tunnel.dns_server)
            del self.tunnels[tunnel.tunnel_id]

            # Try to clean up stderr log
            try:
                os.unlink(tunnel.stderr_path)
            except Exception:
                pass

            # Try reserve resolvers until one works
            while self.reserve_resolvers:
                new_resolver = self.reserve_resolvers.popleft()
                if new_resolver in self.dead_resolvers:
                    continue

                tid = self._next_id
                self._next_id += 1
                new_tunnel = await self._spawn_tunnel(tid, new_resolver)
                if new_tunnel:
                    self.tunnels[new_tunnel.tunnel_id] = new_tunnel
                    logger.info(f"Replacement tunnel #{tid} ({new_resolver}) is up")
                    return
                else:
                    self.dead_resolvers.add(new_resolver)

            logger.warning("No reserve resolvers left for replacement")

    async def fill_up(self):
        """Try to bring tunnel count up to max_tunnels from reserve pool."""
        async with self._lock:
            while len(self.tunnels) < self.max_tunnels and self.reserve_resolvers:
                resolver = self.reserve_resolvers.popleft()
                if resolver in self.dead_resolvers:
                    continue

                tid = self._next_id
                self._next_id += 1
                tunnel = await self._spawn_tunnel(tid, resolver)
                if tunnel:
                    self.tunnels[tunnel.tunnel_id] = tunnel
                    logger.info(
                        f"Fill-up: tunnel #{tid} ({resolver}) is up "
                        f"[{len(self.tunnels)}/{self.max_tunnels}]"
                    )
                else:
                    self.dead_resolvers.add(resolver)

            healthy = len(self.get_healthy_tunnels())
            if healthy < self.max_tunnels and not self.reserve_resolvers:
                logger.warning(
                    f"Fill-up done: {healthy}/{self.max_tunnels} healthy, "
                    f"no reserves left"
                )

    async def revive_dead_resolvers(self):
        """Move dead resolvers back into the reserve queue for re-testing.

        Dead resolvers are DNS servers that previously failed to spawn or
        whose tunnels died.  Network conditions change over time, so we
        periodically give them another chance.
        """
        async with self._lock:
            if not self.dead_resolvers:
                return
            revived = list(self.dead_resolvers)
            random.shuffle(revived)
            for r in revived:
                self.reserve_resolvers.append(r)
            count = len(self.dead_resolvers)
            self.dead_resolvers.clear()
            logger.info(
                f"Revived {count} dead resolver(s) back into reserve pool "
                f"(reserve queue now {len(self.reserve_resolvers)})"
            )

    async def stop_all(self):
        """Kill all dnstt-client processes."""
        for tunnel in list(self.tunnels.values()):
            if tunnel.process and tunnel.is_alive():
                self._kill_process(tunnel.process)
            try:
                os.unlink(tunnel.stderr_path)
            except Exception:
                pass
        self.tunnels.clear()
        logger.info("All tunnels stopped")


# ─── SOCKS5 Protocol Helpers ─────────────────────────────────────────────────


async def socks5_read_addr(
    reader: asyncio.StreamReader,
) -> Tuple[int, bytes, int, str]:
    """
    Read a SOCKS5 address (ATYP + ADDR + PORT) from a stream.
    Returns (atyp, raw_addr_bytes_without_atyp, port, human_readable_addr).
    """
    atyp = (await reader.readexactly(1))[0]

    if atyp == SOCKS5_ATYP_IPV4:
        raw = await reader.readexactly(4)
        addr_str = socket.inet_ntoa(raw)
    elif atyp == SOCKS5_ATYP_DOMAIN:
        length = (await reader.readexactly(1))[0]
        domain_bytes = await reader.readexactly(length)
        raw = bytes([length]) + domain_bytes
        addr_str = domain_bytes.decode("ascii", errors="replace")
    elif atyp == SOCKS5_ATYP_IPV6:
        raw = await reader.readexactly(16)
        addr_str = socket.inet_ntop(socket.AF_INET6, raw)
    else:
        raise ValueError(f"Unknown SOCKS5 address type: {atyp}")

    port_raw = await reader.readexactly(2)
    port = struct.unpack("!H", port_raw)[0]

    return atyp, raw, port, addr_str


def socks5_pack_addr(atyp: int, raw: bytes, port: int) -> bytes:
    """Pack SOCKS5 address bytes: ATYP + RAW_ADDR + PORT."""
    return bytes([atyp]) + raw + struct.pack("!H", port)


def socks5_reply(
    rep: int,
    atyp: int = SOCKS5_ATYP_IPV4,
    addr: bytes = b"\x00\x00\x00\x00",
    port: int = 0,
) -> bytes:
    """Build a SOCKS5 reply packet."""
    return bytes([SOCKS5_VER, rep, 0x00, atyp]) + addr + struct.pack("!H", port)


async def socks5_read_reply_addr(
    reader: asyncio.StreamReader,
) -> Tuple[int, bytes, bytes]:
    """
    Read the bound-address portion of a SOCKS5 reply (ATYP + ADDR + PORT).
    Returns (atyp, addr_raw, port_raw) as raw bytes for forwarding.
    """
    atyp = (await reader.readexactly(1))[0]

    if atyp == SOCKS5_ATYP_IPV4:
        addr_raw = await reader.readexactly(4)
    elif atyp == SOCKS5_ATYP_DOMAIN:
        length = (await reader.readexactly(1))[0]
        addr_raw = bytes([length]) + await reader.readexactly(length)
    elif atyp == SOCKS5_ATYP_IPV6:
        addr_raw = await reader.readexactly(16)
    else:
        addr_raw = b""

    port_raw = await reader.readexactly(2)
    return atyp, addr_raw, port_raw


# ─── SOCKS5 Proxy Server ─────────────────────────────────────────────────────


class Socks5Server:
    """Async SOCKS5 proxy that distributes connections across dnstt tunnels."""

    def __init__(self, pool: TunnelPool, listen_host: str, listen_port: int):
        self.pool = pool
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.server: Optional[asyncio.AbstractServer] = None
        self.total_connections = 0
        self.active_connections = 0
        self._shutdown = False
        # Dedup "no healthy tunnels" warnings
        self._last_no_tunnel_log: float = 0.0
        self._no_tunnel_suppressed: int = 0

    def _log_no_tunnel(self, target_desc: str):
        """Rate-limited 'no healthy tunnels' warning (max once per 10s)."""
        now = time.time()
        if now - self._last_no_tunnel_log < 10.0:
            self._no_tunnel_suppressed += 1
            return
        extra = ""
        if self._no_tunnel_suppressed > 0:
            extra = f" (+{self._no_tunnel_suppressed} suppressed)"
            self._no_tunnel_suppressed = 0
        self._last_no_tunnel_log = now
        logger.warning(f"No healthy tunnels for {target_desc}{extra}")

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle_client, self.listen_host, self.listen_port
        )
        logger.info(f"SOCKS5 proxy listening on {self.listen_host}:{self.listen_port}")

    async def stop(self):
        self._shutdown = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        logger.info("SOCKS5 proxy stopped")

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ):
        """Handle one incoming SOCKS5 connection."""
        if self._shutdown:
            client_writer.close()
            return

        self.total_connections += 1
        self.active_connections += 1

        upstream_writer: Optional[asyncio.StreamWriter] = None
        tunnel: Optional[DnsttTunnel] = None

        try:
            # ── SOCKS5 client greeting ──
            header = await asyncio.wait_for(client_reader.readexactly(2), timeout=10)
            ver, nmethods = header
            if ver != SOCKS5_VER:
                return

            methods = await asyncio.wait_for(
                client_reader.readexactly(nmethods), timeout=5
            )

            if SOCKS5_AUTH_NONE not in methods:
                client_writer.write(bytes([SOCKS5_VER, 0xFF]))
                await client_writer.drain()
                return

            # Accept no-auth
            client_writer.write(bytes([SOCKS5_VER, SOCKS5_AUTH_NONE]))
            await client_writer.drain()

            # ── SOCKS5 CONNECT request ──
            req_header = await asyncio.wait_for(
                client_reader.readexactly(3), timeout=10
            )
            ver, cmd, rsv = req_header

            if cmd != SOCKS5_CMD_CONNECT:
                client_writer.write(socks5_reply(SOCKS5_REP_CMD_NOT_SUPPORTED))
                await client_writer.drain()
                return

            atyp, addr_raw, port, addr_str = await socks5_read_addr(client_reader)
            target_desc = f"{addr_str}:{port}"

            # ── Connect through tunnel with retry ──
            tried: Set[int] = set()
            upstream_reader: Optional[asyncio.StreamReader] = None
            up_atyp = SOCKS5_ATYP_IPV4
            up_addr_raw = b"\x00\x00\x00\x00"
            up_port_raw = b"\x00\x00"

            for attempt in range(MAX_RETRIES + 1):
                # Pick a tunnel
                if attempt == 0:
                    tunnel = self.pool.get_best_tunnel()
                else:
                    tunnel = self.pool.get_alt_tunnel(tried)

                # If no tunnel available, wait briefly — one may recover
                if tunnel is None:
                    waited = 0.0
                    while waited < NO_TUNNEL_WAIT:
                        await asyncio.sleep(NO_TUNNEL_POLL)
                        waited += NO_TUNNEL_POLL
                        if attempt == 0:
                            tunnel = self.pool.get_best_tunnel()
                        else:
                            tunnel = self.pool.get_alt_tunnel(tried)
                        if tunnel is not None:
                            break

                if tunnel is None:
                    self._log_no_tunnel(target_desc)
                    client_writer.write(socks5_reply(SOCKS5_REP_GENERAL))
                    await client_writer.drain()
                    return

                tried.add(tunnel.tunnel_id)

                try:
                    # Connect to dnstt's SOCKS5 port
                    upstream_reader, upstream_writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", tunnel.socks_port),
                        timeout=5.0,
                    )

                    # SOCKS5 greeting to upstream dnstt
                    upstream_writer.write(b"\x05\x01\x00")
                    await upstream_writer.drain()

                    up_greeting = await asyncio.wait_for(
                        upstream_reader.readexactly(2), timeout=10
                    )
                    if (
                        up_greeting[0] != SOCKS5_VER
                        or up_greeting[1] != SOCKS5_AUTH_NONE
                    ):
                        raise ConnectionError("Upstream SOCKS5 auth mismatch")

                    # Forward CONNECT request to upstream
                    connect_req = bytes(
                        [SOCKS5_VER, SOCKS5_CMD_CONNECT, 0x00]
                    ) + socks5_pack_addr(atyp, addr_raw, port)
                    upstream_writer.write(connect_req)
                    await upstream_writer.drain()

                    # Read upstream CONNECT reply
                    up_reply_hdr = await asyncio.wait_for(
                        upstream_reader.readexactly(3), timeout=60
                    )
                    up_ver, up_rep, up_rsv = up_reply_hdr

                    # Read the bound address from the reply
                    up_atyp, up_addr_raw, up_port_raw = await socks5_read_reply_addr(
                        upstream_reader
                    )

                    if up_rep != SOCKS5_REP_SUCCESS:
                        raise ConnectionError(
                            f"Upstream CONNECT rejected: rep=0x{up_rep:02x}"
                        )

                    # Success
                    logger.debug(
                        f"#{tunnel.tunnel_id} -> {target_desc} (attempt {attempt + 1})"
                    )
                    break

                except (
                    asyncio.TimeoutError,
                    asyncio.IncompleteReadError,
                    ConnectionError,
                    OSError,
                ) as e:
                    logger.debug(
                        f"Tunnel #{tunnel.tunnel_id} failed for {target_desc}: {e}"
                    )
                    if tunnel:
                        tunnel.stats.failed_connections += 1
                    # Clean up failed upstream connection
                    if upstream_writer:
                        try:
                            upstream_writer.close()
                        except Exception:
                            pass
                    upstream_reader = None
                    upstream_writer = None
                    tunnel = None
                    continue

            # All retries exhausted?
            if upstream_writer is None or tunnel is None:
                logger.warning(f"All retries failed for {target_desc}")
                client_writer.write(socks5_reply(SOCKS5_REP_HOST_UNREACH))
                await client_writer.drain()
                return

            # ── Send SOCKS5 success reply to client ──
            reply = (
                bytes([SOCKS5_VER, SOCKS5_REP_SUCCESS, 0x00, up_atyp])
                + up_addr_raw
                + up_port_raw
            )
            client_writer.write(reply)
            await client_writer.drain()

            # ── Bidirectional relay ──
            assert upstream_reader is not None
            assert upstream_writer is not None
            tunnel.stats.total_connections += 1
            tunnel.stats.active_connections += 1
            try:
                await self._relay(
                    client_reader,
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                    tunnel,
                )
            finally:
                tunnel.stats.active_connections -= 1

        except (
            asyncio.IncompleteReadError,
            asyncio.TimeoutError,
            ConnectionError,
            OSError,
        ):
            pass
        except Exception as e:
            logger.debug(f"Connection handler error: {type(e).__name__}: {e}")
        finally:
            self.active_connections -= 1
            # Close client
            try:
                if not client_writer.is_closing():
                    client_writer.close()
            except Exception:
                pass
            # Close upstream if still open
            if upstream_writer:
                try:
                    if not upstream_writer.is_closing():
                        upstream_writer.close()
                except Exception:
                    pass

    async def _relay(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
        tunnel: DnsttTunnel,
    ):
        """Bidirectional data relay between client and upstream tunnel."""

        async def pipe(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
            direction: str,
        ):
            try:
                while True:
                    data = await reader.read(RELAY_BUF)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
                    if direction == "tx":
                        tunnel.stats.bytes_tx += len(data)
                    else:
                        tunnel.stats.bytes_rx += len(data)
            except (
                asyncio.CancelledError,
                ConnectionError,
                OSError,
                BrokenPipeError,
            ):
                pass

        t1 = asyncio.create_task(pipe(c_reader, u_writer, "tx"))
        t2 = asyncio.create_task(pipe(u_reader, c_writer, "rx"))

        done, pending = await asyncio.wait(
            {t1, t2}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Close both sides
        for w in (u_writer, c_writer):
            try:
                if not w.is_closing():
                    w.close()
            except Exception:
                pass


# ─── Health Monitor ───────────────────────────────────────────────────────────


class HealthMonitor:
    """Periodically probes each tunnel via SOCKS5 CONNECT to verify it works."""

    def __init__(
        self,
        pool: TunnelPool,
        interval: float = 30.0,
        revive_interval: float = 300.0,
    ):
        self.pool = pool
        self.interval = interval
        self.revive_interval = revive_interval
        self._task: Optional[asyncio.Task] = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        # Short delay before first check so tunnels have time to warm up
        await asyncio.sleep(min(self.interval, 15))
        last_revive = time.time()
        while True:
            try:
                await self._check_all()

                # Periodically revive dead resolvers so they get another chance
                now = time.time()
                if (
                    self.pool.dead_resolvers
                    and now - last_revive >= self.revive_interval
                ):
                    last_revive = now
                    await self.pool.revive_dead_resolvers()

                # After health checks, try to fill up if below max
                if (
                    len(self.pool.tunnels) < self.pool.max_tunnels
                    and self.pool.reserve_resolvers
                ):
                    await self.pool.fill_up()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Health monitor error: {e}")
            await asyncio.sleep(self.interval)

    async def _check_all(self):
        tunnels = list(self.pool.tunnels.values())
        tasks = [self._check_one(t) for t in tunnels]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_one(self, tunnel: DnsttTunnel):
        """Check a single tunnel's health by doing a SOCKS5 CONNECT probe."""

        # First: check if the OS process is even running
        if not tunnel.is_alive():
            logger.warning(
                f"Tunnel #{tunnel.tunnel_id} ({tunnel.dns_server}) process dead"
            )
            tunnel.healthy = False
            tunnel.consecutive_failures = MAX_CONSECUTIVE_FAILURES
            await self.pool.replace_tunnel(tunnel)
            return

        writer = None
        start = time.time()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", tunnel.socks_port),
                timeout=5.0,
            )

            # SOCKS5 greeting
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            greeting = await asyncio.wait_for(reader.readexactly(2), timeout=10)
            if greeting != b"\x05\x00":
                raise ConnectionError("Bad SOCKS5 greeting from tunnel")

            # SOCKS5 CONNECT to health target
            domain = HEALTH_TARGET_HOST.encode("ascii")
            req = (
                bytes(
                    [
                        SOCKS5_VER,
                        SOCKS5_CMD_CONNECT,
                        0x00,
                        SOCKS5_ATYP_DOMAIN,
                        len(domain),
                    ]
                )
                + domain
                + struct.pack("!H", HEALTH_TARGET_PORT)
            )
            writer.write(req)
            await writer.drain()

            # Read CONNECT reply header (VER, REP, RSV)
            reply_hdr = await asyncio.wait_for(reader.readexactly(3), timeout=45)
            ver, rep, rsv = reply_hdr

            # Read and discard bound address
            await socks5_read_reply_addr(reader)

            elapsed = time.time() - start

            if rep == SOCKS5_REP_SUCCESS:
                tunnel.latency = elapsed
                tunnel.healthy = True
                tunnel.consecutive_failures = 0
                tunnel.last_health_check = time.time()
            else:
                raise ConnectionError(f"CONNECT failed: rep=0x{rep:02x}")

        except (
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
        ) as e:
            tunnel.consecutive_failures += 1
            tunnel.last_health_check = time.time()

            if tunnel.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                if tunnel.healthy:
                    logger.warning(
                        f"Tunnel #{tunnel.tunnel_id} ({tunnel.dns_server}) "
                        f"marked unhealthy after "
                        f"{tunnel.consecutive_failures} failures"
                    )
                tunnel.healthy = False
                await self.pool.replace_tunnel(tunnel)
            else:
                logger.info(
                    f"Tunnel #{tunnel.tunnel_id} health check failed "
                    f"({tunnel.consecutive_failures}/"
                    f"{MAX_CONSECUTIVE_FAILURES}): {e}"
                )
        finally:
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass


# ─── ANSI Styles & Helpers ────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


class _S:
    """ANSI escape sequences for terminal styling."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"


def _vlen(s: str) -> int:
    """Visible length of a string (strips ANSI escape codes)."""
    return len(_ANSI_RE.sub("", s))


def _pad(s: str, width: int, align: str = "<") -> str:
    """Pad a string (possibly containing ANSI codes) to a visible width."""
    gap = width - _vlen(s)
    if gap <= 0:
        return s
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


# Box-drawing characters
_H, _V = "\u2500", "\u2502"
_TL, _TR, _BL, _BR = "\u250c", "\u2510", "\u2514", "\u2518"
_LT, _RT = "\u251c", "\u2524"


# ─── Live Dashboard ───────────────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    """Format byte count for display."""
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    else:
        return f"{n / (1024 * 1024 * 1024):.2f}G"


def _fmt_rate(bps: float) -> str:
    """Format bytes-per-second throughput rate."""
    if bps < 1.0:
        return "0B/s"
    return _fmt_bytes(int(bps)) + "/s"


def _fmt_age(seconds: float) -> str:
    """Compact age display that fits within 7 chars: 5m02s, 59m59s, 2h31m, 10h5m."""
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    if h < 100:
        return f"{h}h{m:02d}m"
    d, h2 = divmod(h, 24)
    return f"{d}d{h2:02d}h"


def _fmt_uptime(seconds: float) -> str:
    """Format uptime as HH:MM:SS."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class Dashboard:
    """Rich live terminal dashboard with colors, rates, and box-drawing borders."""

    # Fixed column definitions: (header, width, align)
    # DNS Server column is inserted dynamically based on terminal width.
    _COLS_FIXED = [
        ("#", 3, ">"),
        ("Port", 5, ">"),
        ("Health", 6, "^"),
        ("Lat", 5, ">"),
        ("Act", 3, ">"),
        ("Up", 7, ">"),
        ("Down", 7, ">"),
        ("Age", 7, ">"),
    ]
    _FIXED_WIDTH_SUM = sum(w for _, w, _ in _COLS_FIXED)  # 43

    def __init__(
        self,
        pool: TunnelPool,
        socks_server: Socks5Server,
        log_handler: RingBufferHandler,
        interval: float = 5.0,
        listen_addr: str = "127.0.0.1:8080",
    ):
        self.pool = pool
        self.socks = socks_server
        self.log_handler = log_handler
        self.interval = interval
        self.listen_addr = listen_addr
        self.start_time = time.time()
        self._task: Optional[asyncio.Task] = None
        # Rate tracking: {tunnel_id: (timestamp, bytes_tx, bytes_rx)}
        self._prev_snap: Dict[int, Tuple[float, int, int]] = {}
        self._rates: Dict[int, Tuple[float, float]] = {}  # tx_rate, rx_rate
        self._total_tx_rate: float = 0.0
        self._total_rx_rate: float = 0.0

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Show cursor & clear
        sys.stdout.write(f"{_S.SHOW_CURSOR}\033[2J\033[H")
        sys.stdout.flush()

    async def _run(self):
        sys.stdout.write(_S.HIDE_CURSOR)
        sys.stdout.flush()
        while True:
            try:
                self._update_rates()
                self._draw()
            except Exception:
                pass
            await asyncio.sleep(self.interval)

    # ── Rate tracking ──

    def _update_rates(self):
        """Compute per-tunnel and aggregate throughput rates."""
        now = time.time()
        total_tx = total_rx = 0.0
        new_snap: Dict[int, Tuple[float, int, int]] = {}
        for tid, t in self.pool.tunnels.items():
            tx, rx = t.stats.bytes_tx, t.stats.bytes_rx
            new_snap[tid] = (now, tx, rx)
            if tid in self._prev_snap:
                pt, ptx, prx = self._prev_snap[tid]
                dt = now - pt
                if dt > 0:
                    r_tx = max(0.0, (tx - ptx) / dt)
                    r_rx = max(0.0, (rx - prx) / dt)
                    self._rates[tid] = (r_tx, r_rx)
                    total_tx += r_tx
                    total_rx += r_rx
            else:
                self._rates[tid] = (0.0, 0.0)
        self._prev_snap = new_snap
        self._total_tx_rate = total_tx
        self._total_rx_rate = total_rx
        # Clean up removed tunnels
        for tid in set(self._rates) - set(self.pool.tunnels):
            del self._rates[tid]

    # ── Layout helpers ──

    def _hline(self, w: int, left: str, right: str) -> str:
        """Full-width horizontal border line."""
        return left + _H * (w - 2) + right

    def _row(self, text: str, w: int) -> str:
        """Bordered content row: \u2502 <text padded to inner width> \u2502"""
        inner = w - 4  # accounts for "\u2502 " prefix and " \u2502" suffix
        return f"{_V} {_pad(text, inner)} {_V}"

    def _table_row(self, cells: List[Tuple[str, int, str]], w: int) -> str:
        """Build a bordered row from (content, width, align) cells."""
        parts = [_pad(c, cw, a) for c, cw, a in cells]
        return self._row("  ".join(parts), w)

    def _table_rule(self, col_widths: List[int], w: int) -> str:
        """Thin underline row aligning with column positions."""
        parts = [_H * cw for cw in col_widths]
        return self._row("  ".join(parts), w)

    # ── Main draw ──

    def _draw(self):
        tunnels = sorted(self.pool.tunnels.values(), key=lambda t: t.tunnel_id)
        healthy = sum(1 for t in tunnels if t.healthy)
        total_tx = sum(t.stats.bytes_tx for t in tunnels)
        total_rx = sum(t.stats.bytes_rx for t in tunnels)
        total_conns = sum(t.stats.total_connections for t in tunnels)
        active_conns = sum(t.stats.active_connections for t in tunnels)
        failed_conns = sum(t.stats.failed_connections for t in tunnels)
        uptime = time.time() - self.start_time
        now = time.time()

        try:
            term_w = shutil.get_terminal_size().columns
        except Exception:
            term_w = 100
        W = max(80, min(term_w, 140))

        # Dynamic DNS column width:
        # inner = W-4, col separators "  "*8 = 16
        # dns_w = inner - 16 - fixed_sum = W - 4 - 16 - 43 = W - 63
        dns_w = max(16, W - 63)

        # Build ordered column specs with DNS inserted after #
        col_specs: List[Tuple[str, int, str]] = [self._COLS_FIXED[0]]
        col_specs.append(("DNS Server", dns_w, "<"))
        col_specs.extend(self._COLS_FIXED[1:])  # Port .. Age
        col_widths = [cw for _, cw, _ in col_specs]

        S = _S
        out: List[str] = []
        out.append("\033[H")  # cursor home (no clear — we overwrite in place)

        # ── Top border ──
        out.append(self._hline(W, _TL, _TR))

        # ── Title bar ──
        title = f"{S.BOLD}{S.CYAN}dnstt-balancer{S.RESET}"
        info = f"Listen: {S.BOLD}{self.listen_addr}{S.RESET}"
        up = f"Up: {S.GREEN}{_fmt_uptime(uptime)}{S.RESET}"
        out.append(self._row(f"{title}    {info}    {up}", W))

        out.append(self._hline(W, _LT, _RT))

        # ── Summary row 1: health & connections ──
        hc = (
            S.GREEN
            if healthy == len(tunnels) and healthy > 0
            else (S.YELLOW if healthy > 0 else S.RED)
        )
        parts = [
            f"{hc}{S.BOLD}{healthy}/{len(tunnels)}{S.RESET} Healthy",
            f"{S.CYAN}{active_conns}{S.RESET} Active",
            f"{total_conns:,} Served",
        ]
        if failed_conns:
            parts.append(f"{S.RED}{failed_conns:,}{S.RESET} Failed")
        parts.append(f"{len(self.pool.reserve_resolvers)} Reserve")
        out.append(self._row("    ".join(parts), W))

        # ── Summary row 2: traffic ──
        tx_s = (
            f"{S.GREEN}\u25b2{S.RESET} "
            f"{_fmt_bytes(total_tx)} ({_fmt_rate(self._total_tx_rate)})"
        )
        rx_s = (
            f"{S.CYAN}\u25bc{S.RESET} "
            f"{_fmt_bytes(total_rx)} ({_fmt_rate(self._total_rx_rate)})"
        )
        dead = len(self.pool.dead_resolvers)
        dead_s = f"{S.RED}{dead}{S.RESET}" if dead else str(dead)
        out.append(self._row(f"{tx_s}    {rx_s}    {dead_s} dead resolvers", W))

        # ── Table header ──
        out.append(self._hline(W, _LT, _RT))
        header_cells = [(f"{S.BOLD}{h}{S.RESET}", cw, a) for h, cw, a in col_specs]
        out.append(self._table_row(header_cells, W))
        out.append(self._table_rule(col_widths, W))

        # ── Table data rows ──
        for t in tunnels:
            # Health indicator
            if t.healthy:
                health = f"{S.GREEN}\u25cf OK{S.RESET}"
            else:
                health = f"{S.RED}\u25cb --{S.RESET}"

            # Latency (color-coded)
            if t.latency < float("inf"):
                if t.latency < 2.0:
                    lat = f"{S.GREEN}{t.latency:.1f}s{S.RESET}"
                elif t.latency < 5.0:
                    lat = f"{S.YELLOW}{t.latency:.1f}s{S.RESET}"
                else:
                    lat = f"{S.RED}{t.latency:.1f}s{S.RESET}"
            else:
                lat = f"{S.DIM}  -{S.RESET}"

            # Active connections
            act = (
                f"{S.CYAN}{t.stats.active_connections}{S.RESET}"
                if t.stats.active_connections > 0
                else f"{S.DIM}0{S.RESET}"
            )

            # DNS server (truncate if needed)
            dns = t.dns_server
            if len(dns) > dns_w:
                dns = dns[: dns_w - 1] + "\u2026"

            # Age
            age = _fmt_age(now - t.started_at) if t.started_at > 0 else "-"

            cells: List[Tuple[str, int, str]] = [
                (str(t.tunnel_id), 3, ">"),
                (dns, dns_w, "<"),
                (str(t.socks_port), 5, ">"),
                (health, 6, "^"),
                (lat, 5, ">"),
                (act, 3, ">"),
                (_fmt_bytes(t.stats.bytes_tx), 7, ">"),
                (_fmt_bytes(t.stats.bytes_rx), 7, ">"),
                (age, 7, ">"),
            ]
            out.append(self._table_row(cells, W))

        # ── Events ──
        out.append(self._hline(W, _LT, _RT))
        recent = self.log_handler.get_recent(6)
        if recent:
            out.append(self._row(f"{S.BOLD}Events{S.RESET}", W))
            ev_w = W - 8  # account for border + indent
            for msg in recent:
                display = msg
                if len(msg) > ev_w:
                    display = msg[: ev_w - 3] + "..."
                # Color by log level
                if " ERROR " in msg or " CRITICAL " in msg:
                    display = f"{S.RED}{display}{S.RESET}"
                elif " WARNING " in msg:
                    display = f"{S.YELLOW}{display}{S.RESET}"
                out.append(self._row(f"  {display}", W))

        # ── Footer ──
        out.append(self._hline(W, _LT, _RT))
        foot = f"{S.DIM}Ctrl+C to exit    Refresh: {self.interval:.0f}s{S.RESET}"
        out.append(self._row(foot, W))
        out.append(self._hline(W, _BL, _BR))

        # Pad with blank lines to fill the terminal and erase any leftover
        try:
            term_h = shutil.get_terminal_size().lines
        except Exception:
            term_h = 40
        drawn = len(out) - 1  # first entry is just the \033[H escape
        for _ in range(max(0, term_h - drawn - 1)):
            out.append(" " * W)

        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()


# ─── Main Orchestrator ────────────────────────────────────────────────────────


class DnsttBalancer:
    """Top-level: spawns tunnels, proxy, health checks, dashboard."""

    def __init__(self, args):
        self.args = args
        self.resolvers = self._load_resolvers(args.dns_list)
        if not self.resolvers:
            print(
                f"Error: no resolvers found in {args.dns_list}",
                file=sys.stderr,
            )
            sys.exit(1)

        self.ring_handler = RingBufferHandler(capacity=100)
        self._setup_logging()

        self.pool = TunnelPool(
            dnstt_path=args.dnstt,
            resolvers=self.resolvers,
            pubkey=args.pubkey,
            domain=args.domain,
            dns_port=args.dns_port,
            protocol=args.protocol,
            utls=args.utls,
            max_tunnels=args.max_tunnels,
            startup_wait=args.startup_wait,
        )

        listen_parts = args.listen.rsplit(":", 1)
        self.listen_host = listen_parts[0]
        self.listen_port = int(listen_parts[1])

        self.socks_server = Socks5Server(self.pool, self.listen_host, self.listen_port)
        self.health_monitor = HealthMonitor(
            self.pool, args.health_interval, args.revive_interval
        )

        self.dashboard: Optional[Dashboard] = None
        if not args.no_dashboard:
            self.dashboard = Dashboard(
                self.pool,
                self.socks_server,
                self.ring_handler,
                interval=args.tui_interval,
                listen_addr=args.listen,
            )

    def _load_resolvers(self, path: str) -> List[str]:
        resolvers = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                resolvers.append(line)
        return resolvers

    def _setup_logging(self):
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
        )

        # Ring buffer always active (feeds dashboard)
        self.ring_handler.setFormatter(fmt)
        self.ring_handler.setLevel(logging.INFO)
        logger.addHandler(self.ring_handler)

        if self.args.no_dashboard:
            # Console output when dashboard is disabled
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(fmt)
            ch.setLevel(logging.INFO)
            logger.addHandler(ch)

        if self.args.log_file:
            fh = logging.FileHandler(self.args.log_file)
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            fh.setLevel(logging.DEBUG)
            logger.addHandler(fh)

        logger.setLevel(logging.DEBUG)

    async def run(self):
        """Main entry: spawn tunnels, start services, wait for shutdown."""
        logger.info("Starting dnstt-balancer...")
        logger.info(
            f"  Resolvers: {len(self.resolvers)} loaded from {self.args.dns_list}"
        )
        logger.info(f"  Max tunnels: {self.args.max_tunnels}")
        logger.info(f"  Protocol: {self.args.protocol}, DNS port: {self.args.dns_port}")
        if self.args.utls:
            logger.info(f"  uTLS: {self.args.utls}")
        logger.info(f"  Listen: {self.args.listen}")

        # Spawn tunnels
        await self.pool.spawn_all()

        if not self.pool.get_healthy_tunnels():
            logger.error("No healthy tunnels available. Exiting.")
            await self.pool.stop_all()
            return

        # Start SOCKS5 proxy
        await self.socks_server.start()

        # Start health monitor
        self.health_monitor.start()

        # Start dashboard
        if self.dashboard:
            self.dashboard.start()

        # Wait for shutdown signal
        self._shutdown_event = asyncio.Event()
        loop = asyncio.get_event_loop()

        def _signal_handler():
            logger.info("Shutdown signal received")
            self._shutdown_event.set()

        if IS_WINDOWS:
            # Windows does not support loop.add_signal_handler; use
            # signal.signal which works from the main thread.
            signal.signal(signal.SIGINT, lambda s, f: _signal_handler())
            signal.signal(signal.SIGTERM, lambda s, f: _signal_handler())
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _signal_handler)

        try:
            await self._shutdown_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

        # ── Graceful shutdown ──
        await self._shutdown()

    async def _shutdown(self):
        """Perform graceful shutdown of all components."""
        logger.info("Shutting down...")

        if self.dashboard:
            await self.dashboard.stop()

        await self.health_monitor.stop()
        await self.socks_server.stop()

        # Brief drain period for active connections
        if self.socks_server.active_connections > 0:
            logger.info(
                f"Waiting for {self.socks_server.active_connections} "
                f"active connections to drain..."
            )
            for _ in range(50):  # up to 5 seconds
                if self.socks_server.active_connections <= 0:
                    break
                await asyncio.sleep(0.1)

        await self.pool.stop_all()
        self._print_final_stats()

    def _print_final_stats(self):
        tunnels = list(self.pool.tunnels.values()) if self.pool.tunnels else []
        total_tx = sum(t.stats.bytes_tx for t in tunnels)
        total_rx = sum(t.stats.bytes_rx for t in tunnels)
        total_conns = self.socks_server.total_connections
        uptime = time.time() - self.dashboard.start_time if self.dashboard else 0

        S = _S
        W = 52
        hl = _TL + _H * (W - 2) + _TR
        ml = _LT + _H * (W - 2) + _RT
        bl = _BL + _H * (W - 2) + _BR

        def row(text: str) -> str:
            return f"{_V} {_pad(text, W - 4)} {_V}"

        lines = [
            "",
            hl,
            row(f"{S.BOLD}{S.CYAN}Final Statistics{S.RESET}"),
            ml,
            row(f"Uptime:          {S.GREEN}{_fmt_uptime(uptime)}{S.RESET}"),
            row(f"Connections:     {S.CYAN}{total_conns:,}{S.RESET}"),
            row(f"Uploaded:        {S.GREEN}{_fmt_bytes(total_tx)}{S.RESET}"),
            row(f"Downloaded:      {S.CYAN}{_fmt_bytes(total_rx)}{S.RESET}"),
            row(
                f"Dead resolvers:  {S.RED if self.pool.dead_resolvers else ''}"
                f"{len(self.pool.dead_resolvers)}{S.RESET}"
            ),
            bl,
        ]
        print("\n".join(lines))


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main():
    # Raise open file limit for many subprocess FDs (Unix only)
    if not IS_WINDOWS:
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            desired = min(hard, max(65536, soft))
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
        except Exception:
            pass

    # Enable ANSI/VT100 escape sequences on Windows 10+
    if IS_WINDOWS:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # STD_OUTPUT_HANDLE = -11
            handle = kernel32.GetStdHandle(-11)
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    p = argparse.ArgumentParser(
        description="Multi-tunnel SOCKS5 load balancer for dnstt-client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python3 dnstt-balancer.py \\
      --dns-list working_dns_servers.txt \\
      --pubkey <pub key> \\
      --domain <domain>

  Then point your browser / Telegram SOCKS5 proxy to 127.0.0.1:8081
""",
    )

    default_dnstt = TunnelPool._default_dnstt_name()
    p.add_argument(
        "--dnstt",
        default=default_dnstt,
        help=f"Path to dnstt-client binary (default: {default_dnstt})",
    )
    p.add_argument(
        "--dns-list",
        required=True,
        help="Text file with DNS resolver IPs, one per line",
    )
    p.add_argument(
        "--pubkey",
        required=True,
        help="dnstt server public key",
    )
    p.add_argument(
        "--domain",
        required=True,
        help="dnstt domain",
    )
    p.add_argument(
        "--dns-port",
        type=int,
        default=53,
        help="DNS port (default: 53)",
    )
    p.add_argument(
        "--protocol",
        choices=["udp", "dot", "doh"],
        default="udp",
        help="DNS transport protocol (default: udp)",
    )
    p.add_argument(
        "--utls",
        default=None,
        help="uTLS client fingerprint (e.g. Chrome_120). Adds -utls flag to dnstt.",
    )
    p.add_argument(
        "--listen",
        default="127.0.0.1:8081",
        help="SOCKS5 proxy listen address:port (default: 127.0.0.1:8080)",
    )
    p.add_argument(
        "--max-tunnels",
        type=int,
        default=15,
        help="Maximum concurrent tunnels (default: 15)",
    )
    p.add_argument(
        "--startup-wait",
        type=float,
        default=6.0,
        help="Seconds to wait for each dnstt-client to start (default: 6.0)",
    )
    p.add_argument(
        "--health-interval",
        type=float,
        default=30.0,
        help="Health check interval in seconds (default: 30.0)",
    )
    p.add_argument(
        "--revive-interval",
        type=float,
        default=300.0,
        help="Seconds between retrying dead resolvers (default: 300 = 5min)",
    )
    p.add_argument(
        "--tui-interval",
        "--stats-interval",
        dest="tui_interval",
        type=float,
        default=2.0,
        help="Dashboard refresh interval in seconds (default: 2.0)",
    )
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable live dashboard (log to stderr instead)",
    )
    p.add_argument(
        "--log-file",
        default=None,
        help="Log to file (recommended when dashboard is active)",
    )

    args = p.parse_args()

    if not os.path.isfile(args.dnstt):
        print(f"Error: dnstt binary not found: {args.dnstt}", file=sys.stderr)
        sys.exit(1)

    if not os.access(args.dnstt, os.X_OK):
        print(
            f"Error: dnstt binary not executable: {args.dnstt}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        asyncio.run(DnsttBalancer(args).run())
    except KeyboardInterrupt:
        # Final fallback: force-kill any remaining dnstt processes
        print("\nForce shutdown...")
        import subprocess as _sp

        try:
            if IS_WINDOWS:
                _sp.run(
                    'taskkill /F /IM "dnstt-client*" 2>NUL',
                    shell=True,
                    timeout=5,
                    capture_output=True,
                )
            else:
                _sp.run(
                    ["pkill", "-f", "dnstt-client.*127.0.0.1:3"],
                    timeout=3,
                    capture_output=True,
                )
        except Exception:
            pass


if __name__ == "__main__":
    main()
