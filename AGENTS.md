# AGENTS.md

One-clone, one-service TAK infrastructure console. `app.py` is a monolith (~78k lines) by deployment design — splitting it is the active decomposition roadmap (ARCHITECTURE.md).

## Architecture

- **Entry**: `app.py` (Flask + gunicorn on `:5001`), started by `start.sh` → systemd unit `takwerx-console`.
- **Privilege**: console runs as `takwerx` (unprivileged). All root ops go through `broker/takwerx_broker.py` via `_sudo_wrap()`.
- **State**: JSON files in `.config/` (mode 600). No database. `load_settings()`/`save_settings()` are the only I/O.
- **Multiplatform**: Ubuntu 22.04, Rocky/RHEL 9, ARM64. Never use bare `apt-get`/`dnf`/`ufw`/`firewall-cmd`.
- **Version**: `VERSION = "..."` near `app.py:968`. Read it, don't guess.

## Critical seams (never bypass)

| Seam | Purpose |
|---|---|
| `_pkg_install(pkgs)` | Package install (apt AND dnf) |
| `_fw_allow(port)` / `_fw_remove(port)` | Firewall rules (ufw AND firewalld) |
| `_sudo_wrap(cmd)` | Every privileged subprocess call |
| `os_type` / `_host_arch()` | OS-family and arch branching |
| `load_settings()` / `save_settings()` | All state reads/writes |

Bypassing a seam is a review failure. `broker/scan_unwrapped_privileged.py` mechanically enforces this.

## Module contract

Modules live in `modules/<key>.py` + `templates/<key>.html`. Registered via `modules/__init__.py` (`register_module()`). The contract:

1. **Imports**: import NOTHING from `app.py`. All seams arrive through the `ctx` dict passed to `register(ctx)`.
2. **Descriptor fields**: `key`, `name`, `description`, `icon`, `route`, `template`, `priority`, `detect`, `deploy`, `uninstall`, `control_map`. See `modules/__init__.py:_validate_descriptor`.
3. **Multiplatform**: Docker images must support both `linux/amd64` and `linux/arm64`, or build from source.

Reference modules: `modules/tvr.py`, `modules/simulator.py`. Full spec: `docs/MODULE-DEVELOPMENT.md`.

## Finding code in app.py

Routes are grouped by URL prefix — grep the prefix, not the feature name:

```bash
grep -nE "@app.route\('/api/takserver" app.py
```

Every `/api/*` route is guarded by `@login_required`.

## File map

| Path | Role |
|---|---|
| `app.py` | Entire backend + UI (~400 routes, deploy engines, health checks) |
| `start.sh` | Installer: OS detect, deps, venv, cert, systemd, non-root provisioning |
| `broker/` | Root-privilege broker + scan/enforcement scripts |
| `modules/` | Registry-based module files (one per module) |
| `templates/` | Jinja2 templates (one per module page + base) |
| `nodered/` | Node-RED flow generator (`build-flows.js`) + deploy pipeline |
| `scripts/` | Standalone operator tools (diagnostics, migration) |
| `static/` | Shared JS/assets |
| `.config/` | Runtime state (gitignored). Auth, settings, SSL certs |
| `docs/` | Mostly gitignored — only `GUARDDOG.md`, `MODULE-DEVELOPMENT.md`, etc. tracked |

## Validation

No CI, no lint, no test suite, no formatter. Verification steps:

- `python3 broker/scan_unwrapped_privileged.py` — finds unwrapped privileged calls
- `python3 -m py_compile modules/<key>.py` — module syntax check
- `python3 -m py_compile app.py` — syntax check

## License

AGPL-3.0-or-later. Every new source file needs an SPDX header. See `CONTRIBUTING.md` for the exact block. Commits must carry `Signed-off-by:`.
