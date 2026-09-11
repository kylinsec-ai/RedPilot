# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Ghost** is a security benchmark evaluation platform with two deployable units:
- **Server** (`packages/ghost`): unified FastAPI app — control plane (task scheduling, job assignment, scoring) *and* observability platform (agent log ingestion, SQLite storage, read-only dashboard + SPA) on a single port (:8000)
- **Worker** (`packages/worker`): Isolated solver containers with VPN + Pi Agent that claim jobs and solve challenges

The project is transitioning from "TsecBench" to "Ghost" — package names use `ghost_*` / `ghost-*`.

## Architecture

### Three-Package Monorepo

```
packages/
├── contracts/    # ghost-contracts — zero-dependency shared contracts
│                 # (vocabulary, snapshot schema, redact, digest, paths, assets)
├── ghost/        # ghost — unified server: ghost.control + ghost.obs + ghost.app
└── worker/       # ghost-worker — orchestration, Pi solver, relay, assignment client
```

`packages/ghost` supersedes the former `packages/core` (ghost-core) and `packages/obs`
(ghost-obs), which were merged into one process/port. Its base install depends only on
`ghost-contracts`; FastAPI et al. live in the `[platform]` extra so the Kali worker image
can install it for `ghost.obs.localserver` without pulling a web stack.

**Dependency direction**: `contracts ← [ghost, worker]`
- `contracts` has **zero dependencies** (stdlib only) — enforced by `packages/contracts/tests/test_purity.py`
- within `ghost`: `ghost.control` never imports `ghost.obs`, and `ghost.obs` never imports `ghost.control`
- `worker` → server (HTTP via SDK / AssignmentClient)
- `worker` → server (HTTP relay + allowed import of `ghost.obs.localserver` for local dashboard)
- `ghost.obs` must NOT import `ghost.control` or `ghost_worker` code

### Data Flow

```
Server (ghost.control) → creates evaluation → generates jobs
       ↓
Worker claims job → Pi Agent solves → submits flags → reports attempt
       ↓                                    ↓
   Telemetry                          Canonical events
   (relay)                            (control outbox)
       ↓                                    ↓
       └────────→  Server (ghost.obs)  ←────┘
                   (SQLite + API + SPA)
```

**Two event streams**:
1. **Canonical events** (authoritative): `attempt.started` / `attempt.completed` from core outbox
2. **Telemetry** (observability): transcript events, live state, roster from worker relay

### Operating Modes

- **Legacy mode** (`WORKER_MODE=legacy`): Worker uses full SDK challenge list
- **Assignment mode** (`WORKER_MODE=assignment`): Worker registers with control plane, claims jobs via lease mechanism

## Development Commands

### Installation

```bash
# Create venv and install all packages in editable mode
python3 -m venv .venv
.venv/bin/pip install -e packages/contracts -e "packages/ghost[platform]" -e packages/worker
```

### Running Tests

```bash
# All tests
pytest

# Specific package
pytest packages/contracts/tests/
pytest packages/ghost/tests/control/
pytest packages/ghost/tests/obs/
pytest packages/worker/tests/

# Run tests with output
pytest -v

# Run specific test file or function
pytest packages/contracts/tests/test_purity.py
pytest packages/ghost/tests/obs/test_canonical_run_close.py -v
```

### Running Components Locally

**Control plane**:
```bash
export GHOST_TASKS_JSON='{"tasks":[...]}'  # or GHOST_CONFIG=/path/to/tasks.json
export GHOST_ADMIN_TOKEN=admin-secret
export GHOST_WORKER_TOKEN=worker-secret
.venv/bin/python main.py
# Runs on http://0.0.0.0:8000
```

**Observability platform**:
```bash
export OBSERVABILITY_TOKEN=obs-secret
export OBSERVABILITY_WEB=$PWD/web
export OBSERVABILITY_DB=$PWD/data/obs.sqlite3
.venv/bin/uvicorn obs.app:app --host 0.0.0.0 --port 8090
```

**Worker** (requires Docker for full setup):
```bash
# Build base image first (Kali + Pi environment)
docker build -f Dockerfile.base -t ghost/kali:latest .

# Run full stack
docker compose up -d --build

# View logs
docker compose logs -f worker
docker compose logs -f control
docker compose logs -f platform

# Stop
docker compose down
```

### Frontend Development

Frontend source is in `frontend/`, built output goes to `web/` (committed to repo).

```bash
cd frontend

# Install dependencies (use npm ci to respect lock file)
npm ci

# Development server with hot reload (proxies /api to localhost:8090)
npm run dev
# Access at http://localhost:5173

# Type check + build for production
npm run build
# Output: ../web/

# Type check only
npm run check
```

**Important**: TypeScript must stay on 6.x (locked in package-lock.json) — `svelte-check` doesn't support TS 7.x yet.

### Docker Image Management

**Upgrading Pi Agent** (critical — must stay current):
```bash
# Check latest version
npm view @earendil-works/pi-coding-agent version

# If Node version requirement changed, update NODE_VERSION in both:
# - Dockerfile.base
# - Dockerfile
# (must match — both extract to /usr/local)

# Rebuild images
docker build -f Dockerfile.base -t ghost/kali:latest .
docker build -f Dockerfile -t ghost-adapter:latest .

# Verify
docker run --rm --env-file .env ghost-adapter:latest pi --version
docker run --rm --env-file .env ghost-adapter:latest pi --print --model $SOLVER_MODEL "Reply with exactly: OK"

# Run worker and verify first challenge has turns > 0
```

## Key Implementation Patterns

### Event Lifecycle & Authority

- **Canonical events** (`attempt.started`, `attempt.completed`) are the **single source of truth** for attempt state
- Core writes these to `platform_events` + `outbox_events` tables atomically
- Obs receives them via `/api/internal/canonical-events` and marks runs with `canonical=1`
- Worker relay telemetry (`run_close`) **cannot overwrite** canonical state
- Same `attempt_id` maps to exactly one `run` row in obs — handled by `append_events()` with row reuse logic

### SQLite Constraints

- Both `core` and `obs` use **SQLite WAL mode** with `workers=1` (single writer per process)
- This is a hard constraint — do not add multi-process workers
- Schema migrations use versioned approach (see `packages/ghost/ghost/obs/db.py` — the `MIGRATIONS` ladder driven by `PRAGMA user_version`)

### Token Security

All token comparisons use `hmac.compare_digest()` for constant-time comparison:
- `BENCHMARK_TOKEN`: challenges API (start/submit/close)
- `X-Worker-Token`: assignment API (claim/heartbeat/complete) 
- `GHOST_ADMIN_TOKEN`: management endpoints (evaluations, VPN)
- `X-Observability-Token`: obs ingest endpoints

**Fail-closed**: If token env vars are unset, endpoints return 503 (not 401/403).

### Observability Trust Boundary

- Obs **read endpoints have no authentication** by default (by design)
- They expose plaintext flags and full agent transcripts
- Docker compose binds obs to `127.0.0.1` by default (localhost only)
- To allow LAN access: set `OBS_HOST_IP=0.0.0.0` (security risk — user must acknowledge)
- Ingest endpoints require `X-Observability-Token`

### Worker Provisioner Patterns

Two provisioner types in `packages/ghost/ghost/control/provisioner.py`:
- `StaticProvisioner`: Uses pre-configured `container_addr` from challenge definition
- `DockerProvisioner`: Spawns containers via `docker run` subprocess

Both implement same interface. Provisioner operations are side-effect heavy — mock in tests.

### VPN Admin Endpoint Security

`/openapi/v1/vpn/*` endpoints (upload/start/stop OpenVPN config) require `GHOST_ADMIN_TOKEN`.

Config upload rejects dangerous directives: `script-security`, `up`, `down`, `plugin`, `route-up`, `tls-verify`, etc.

Always starts OpenVPN with `--script-security 1` (no script execution).

### Pi Skills Knowledge Base

`skills/` directory contains Pi Agent skill definitions (web-recon-toolkit, known-cve-playbook, waf-bypass, sandbox-escape, cloud-security, network-pwn, reverse-engineering).

- Skills use Pi native format (SKILL.md with frontmatter)
- Mounted to `/root/.pi/agent/skills` in worker container
- Auto-discovered by Pi Agent (description always available, content loaded on demand)
- Changes take effect on next challenge without rebuilding image (bind-mounted)
- Skills are also baked into image at build time

Per-challenge `CLAUDE.md` files in workdirs should only contain hard rules (flag format, skill pointers) — general tactics go in skills.

## Common Pitfalls

### Don't Break Dependency Direction

❌ Never `import ghost_worker` or `import ghost.service` from `obs` code  
❌ Never `import obs.ingest` or `import obs.store` from `core` code  
✅ All packages can `import ghost_contracts`  
✅ Worker can `import obs.localserver` (explicitly allowed for local dashboard injection)

### Don't Add Dependencies to Contracts

The `ghost-contracts` package must remain stdlib-only. The purity test will fail if you add any third-party imports.

### Don't Use `workers > 1` for Core or Obs

SQLite WAL + SSE bus requires single-writer process. Use `uvicorn app:app` without `--workers` flag, or explicit `--workers 1`.

### Don't Commit Secrets

Never commit actual tokens, API keys, or VPN configs. Use `.env` (gitignored) for local development. See `.env.example` for template.

### Frontend: Don't Upgrade TypeScript to 7.x

`svelte-check` breaks with TypeScript 7.x. Keep typescript locked at `^6.0.3` in package-lock.json. Use `npm ci` (not `npm install`) to respect the lock.

### Pi Version Must Stay Current

The worker image installs Pi without version pinning (`npm install -g @earendil-works/pi-coding-agent`). After Pi releases:
1. Check changelog for Node version requirements and breaking changes
2. Update `NODE_VERSION` in Dockerfile.base and Dockerfile if needed
3. Rebuild both images
4. Run regression tests before deploying

Failure to upgrade can cause silent failures (e.g., missing session headers causing 0-turn runs).

## File Organization

- `main.py` — Server entry point (control plane + observability, `ghost.app:app`)
- `entrypoint.sh` — Worker container entrypoint (VPN setup + driver launch)
- `packages/*/pyproject.toml` — Package metadata (each package is independently installable)
- `docker-compose.yaml` — Full stack orchestration (server + worker)
- `Dockerfile.base` — Base Kali image with Pi environment
- `Dockerfile.ghost` — Unified server image (control + observability)
- `Dockerfile` — Worker image (extends base)
- `pytest.ini` — Test configuration (root level)
- `web/` — Frontend build output (committed, served by the server)
- `frontend/` — Frontend source (Svelte 5 + TypeScript + Tailwind v4)
- `skills/` — Pi Agent skills knowledge base
- `tools/` — Helper scripts (pw_fetch.py, pw_example.py for Playwright automation)
- `docs/ARCHITECTURE.md` — Detailed architecture RFC with state machines and trust boundaries

## Documentation

- **README.md** — Quick start, environment variables, three-package architecture
- **ARCHITECTURE.md** — Design principles, layer responsibilities, state machines, trust boundaries
- **CHALLENGES_API.md** — Server-side API contract
- **SDK_API.md** — Official SDK usage for platform integration
