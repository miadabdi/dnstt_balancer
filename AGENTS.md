# AGENTS.md

This file defines repository-specific guidance for coding agents and contributors.

## Scope

- Applies to the entire repository rooted at this directory.
- Source of truth for runtime behavior is `dnstt-balancer.py`.
- Human-facing docs live in `README.md` (English) and `README.fa.md` (Persian).

## Project Summary

- Project: `dnstt-balancer`
- Type: Single-file Python application (`dnstt-balancer.py`)
- Purpose: Run multiple `dnstt-client` tunnels and expose one SOCKS5 endpoint with health-aware balancing.
- Dependencies: Python standard library only.

## Key Files

- `dnstt-balancer.py`: Main application (CLI, tunnel pool, SOCKS5 server, health monitor, dashboard).
- `dns.txt`: Example resolver list file.
- `README.md`: Primary English documentation.
- `README.fa.md`: Persian documentation.
- `install_deps.sh`, `install_deps.bat`: Placeholder install scripts (currently no external deps needed).

## Architecture Map

- `TunnelPool`:
  - Spawns and manages `dnstt-client` processes.
  - Tracks active/reserve/dead resolvers.
  - Replaces failed tunnels and refills pool.
- `Socks5Server`:
  - Accepts client SOCKS5 connections.
  - Selects tunnels with latency/load weighting.
  - Retries failed upstream connects across alternate tunnels.
  - Applies per-connection idle timeout in relay.
- `HealthMonitor`:
  - Periodic tunnel probes via SOCKS5 CONNECT.
  - Marks/replaces unhealthy tunnels after consecutive failures.
  - Revives dead resolvers periodically.
  - Optionally recycles old idle tunnels.
- `Dashboard`:
  - Optional live TUI for tunnel health, traffic rates, and recent events.

## Runtime Defaults (Keep Docs Aligned)

Use these runtime defaults when updating docs/examples:

- `--listen`: `127.0.0.1:8081`
- `--max-tunnels`: `15`
- `--startup-wait`: `6.0`
- `--health-interval`: `30.0`
- `--health-timeout`: `15.0`
- `--revive-interval`: `600.0`
- `--tui-interval` / `--stats-interval`: `2.0`
- `--idle-timeout`: `120.0`
- `--recycle-age`: `0` (disabled)
- Health probe target: `www.google.com:443`
- Retry behavior: up to `MAX_RETRIES = 2` additional attempts.
- Unhealthy threshold: `MAX_CONSECUTIVE_FAILURES = 3`.

## Contributor Rules

- Keep the project dependency-free unless explicitly requested.
- Preserve cross-platform behavior (Linux/macOS/Windows paths and process handling).
- Do not silently change CLI flags or defaults; if changed, update:
  - `README.md`
  - `README.fa.md`
  - argparse help text in `dnstt-balancer.py`
- Keep behavior changes localized and documented in the readmes.
- Maintain graceful shutdown semantics and second-signal force-quit behavior.

## Validation Checklist For Changes

Run these checks after relevant edits:

1. `python3 dnstt-balancer.py -h`
2. Verify README flag/default tables match runtime defaults.
3. If routing/health logic changed, confirm `How It Works` sections in both readmes still match.
4. For docs-only changes, ensure `README.md` and `README.fa.md` stay consistent in covered features/options.

## Known Documentation Drift To Watch

- Argparse help strings can drift from actual defaults if default values are edited but help text is not.
- When in doubt, trust actual `add_argument(..., default=...)` values and runtime constants over prose.
