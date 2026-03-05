# dnstt-balancer

Multi-tunnel SOCKS5 load balancer for [dnstt-client](https://www.bamsoftware.com/software/dnstt/).

Spawns multiple `dnstt-client` processes (one per DNS resolver), exposes a single unified SOCKS5 proxy, and distributes connections across healthy tunnels using latency-weighted routing — with automatic health monitoring, dead-resolver revival, retry on failure, and a live terminal dashboard.

## Features

- **Multi-tunnel load balancing** — runs up to N parallel `dnstt-client` tunnels and spreads traffic across them
- **Latency-weighted routing** — faster tunnels get more connections automatically
- **Health monitoring** — periodic SOCKS5 CONNECT probes detect dead tunnels and replace them from a reserve pool
- **Dead-resolver revival** — resolvers that previously failed are periodically retried so the pool doesn't shrink over time
- **Automatic retry** — if a connection fails through one tunnel, it transparently retries on a different one
- **Live TUI dashboard** — color-coded, box-drawing terminal UI showing tunnel health, latency, throughput rates, and recent events
- **Cross-platform** — works on Linux, macOS, and Windows
- **Zero dependencies** — uses only the Python 3 standard library

## Requirements

- **Python 3.8+**
- **`dnstt-client` binary** — the appropriate binary for your platform should be in the working directory (or specify its path with `--dnstt`)
- A text file listing DNS resolver IPs, one per line

## Installation

No third-party packages are needed. If that changes in the future, install scripts are provided:

```bash
# Linux / macOS
./install_deps.sh

# Windows
install_deps.bat
```

## Usage

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <SERVER_PUBLIC_KEY> \
    --domain <YOUR_DOMAIN>
```

Then point your browser, Telegram, or any SOCKS5-aware application to **`127.0.0.1:8081`**.

### DNS List File Format

Plain text, one resolver IP per line. Comments (`#`) and blank lines are ignored:

```
# Google
8.8.8.8
8.8.4.4

# Cloudflare
1.1.1.1
1.0.0.1
```

## Command-Line Options

| Flag                       | Default                      | Description                                                |
| -------------------------- | ---------------------------- | ---------------------------------------------------------- |
| `--dnstt PATH`             | `./dnstt-client-linux-amd64` | Path to `dnstt-client` binary (auto-detected per platform) |
| `--dns-list FILE`          | _(required)_                 | Text file with DNS resolver IPs                            |
| `--pubkey KEY`             | _(required)_                 | dnstt server public key                                    |
| `--domain DOMAIN`          | _(required)_                 | dnstt domain                                               |
| `--dns-port PORT`          | `53`                         | DNS port                                                   |
| `--protocol {udp,dot,doh}` | `udp`                        | DNS transport protocol                                     |
| `--utls FINGERPRINT`       | _(none)_                     | uTLS client fingerprint (e.g. `Chrome_120`)                |
| `--listen HOST:PORT`       | `127.0.0.1:8081`             | SOCKS5 proxy listen address                                |
| `--max-tunnels N`          | `15`                         | Maximum concurrent tunnels                                 |
| `--startup-wait SECS`      | `6.0`                        | Seconds to wait for each tunnel to start                   |
| `--health-interval SECS`   | `30.0`                       | Health check interval                                      |
| `--revive-interval SECS`   | `300.0`                      | Seconds between retrying dead resolvers                    |
| `--stats-interval SECS`    | `5.0`                        | Dashboard refresh interval                                 |
| `--no-dashboard`           | _(off)_                      | Disable live TUI, log to stderr instead                    |
| `--log-file PATH`          | _(none)_                     | Log to file (recommended alongside dashboard)              |

## Examples

Basic usage with UDP:

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <pub key> \
    --domain <domain>
```

DoH with uTLS fingerprinting, 10 tunnels max, custom listen port:

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <KEY> \
    --domain t.example.com \
    --protocol doh \
    --utls Chrome_120 \
    --max-tunnels 10 \
    --listen 127.0.0.1:1080
```

Headless mode with logging:

```bash
python3 dnstt-balancer.py \
    --dns-list dns.txt \
    --pubkey <KEY> \
    --domain t.example.com \
    --no-dashboard \
    --log-file balancer.log
```

## How It Works

1. **Startup** — reads the DNS list, shuffles it, and spawns up to `--max-tunnels` `dnstt-client` processes in parallel. Each gets a unique local SOCKS5 port (starting at 30000).

2. **Proxy** — listens for incoming SOCKS5 connections on the `--listen` address. Each connection is routed to the best available tunnel (weighted by latency and current load). If the upstream connect fails, it retries on a different tunnel (up to 2 retries).

3. **Health checks** — every `--health-interval` seconds, each tunnel is probed with a SOCKS5 CONNECT to `www.gstatic.com:443`. Tunnels that fail 3 consecutive checks are marked unhealthy and replaced from the reserve pool.

4. **Dead-resolver revival** — every `--revive-interval` seconds, previously dead resolvers are moved back into the reserve pool and retried, preventing permanent pool shrinkage.

5. **Shutdown** — Ctrl+C triggers graceful shutdown: stops accepting new connections, waits up to 5 seconds for active connections to drain, kills all `dnstt-client` processes, and prints final statistics.

## Project Structure

```
dnstt-balancer/
├── dnstt-balancer.py      # Main script (single-file, no dependencies)
├── dns.txt                # DNS resolver list
├── requirements.txt       # Empty — stdlib only
├── install_deps.sh        # Linux/macOS dependency installer
├── install_deps.bat       # Windows dependency installer
└── README.md
```

## License

MIT
