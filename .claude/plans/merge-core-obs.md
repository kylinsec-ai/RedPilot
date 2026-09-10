# Implementation Plan: Merge Core + Obs into Unified Ghost Package

## Overview

Merge `packages/core` and `packages/obs` into a single `packages/ghost` package to simplify deployment, reduce HTTP overhead, and unify the API surface while maintaining clear internal boundaries.

## Current State Analysis

### Package Structure
- **packages/core** (ghost-core): 15 Python files in `ghost/` module
  - Control plane, challenges API, scheduling, provisioner, VPN, store
  - Dependencies: fastapi, uvicorn, pydantic, python-dotenv, httpx, ghost-contracts
  - Entry point: `ghost.api:create_app`
  
- **packages/obs** (ghost-obs): 15 Python files in `obs/` module
  - Observability platform: ingest, read API, store, bus, localserver
  - Dependencies: ghost-contracts (base) + fastapi, uvicorn, pydantic, python-dotenv (platform extra)
  - Entry point: `obs.app:create_app`

### Current Dependency Flow
```
contracts ← [core, worker, obs]
```

Worker imports from obs: `obs.localserver` (explicitly allowed for local dashboard)

### Current Deployment
- **control**: Dockerfile.core → port 8000
- **platform**: Dockerfile.platform → port 8090
- **worker**: Dockerfile → includes obs for localserver

### Key Constraint
Worker MUST continue to import `obs.localserver` (stdlib-only module for local dashboard injection)

## Target Architecture

### Three-Package Structure
```
packages/
├── contracts/     # Zero-dependency shared contracts (unchanged)
├── ghost/         # Unified server (core + obs merged)
│   ├── control/   # Control plane logic (from core)
│   ├── obs/       # Observability logic (from obs)
│   ├── app.py     # Unified FastAPI application
│   └── ...
└── worker/        # Worker (unchanged, imports ghost.obs.localserver)
```

### New Dependency Flow
```
contracts ← [ghost, worker]
```

Worker can import `ghost.obs.localserver` (same allowance, new path)

## Implementation Strategy

### Phase 1: Create New Package Structure

**1.1 Create `packages/ghost/` directory**
```
packages/ghost/
├── pyproject.toml          # Merged dependencies
├── ghost/
│   ├── __init__.py         # Package entry point
│   ├── app.py              # Unified FastAPI app
│   ├── control/            # From packages/core/ghost/
│   │   ├── __init__.py
│   │   ├── api.py
│   │   ├── challenges.py
│   │   ├── config.py
│   │   ├── control.py
│   │   ├── errors.py
│   │   ├── models.py
│   │   ├── outbox.py
│   │   ├── provisioner.py
│   │   ├── scheduling.py
│   │   ├── service.py
│   │   ├── store.py
│   │   ├── store_facets.py
│   │   └── vpn.py
│   └── obs/                # From packages/obs/obs/
│       ├── __init__.py
│       ├── bus.py
│       ├── canonical_ingest.py
│       ├── config.py
│       ├── control_proxy.py
│       ├── db.py
│       ├── ingest.py
│       ├── ingest_common.py
│       ├── localserver.py  # CRITICAL: Must remain importable for worker
│       ├── read.py
│       ├── schema.py
│       ├── store.py
│       └── telemetry_ingest.py
└── tests/                  # Merged tests
    ├── control/            # From packages/core/tests/
    └── obs/                # From packages/obs/tests/
```

**1.2 Create `packages/ghost/pyproject.toml`**
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "ghost"
version = "0.1.0"
description = "Ghost security benchmark platform: control plane + observability"
requires-python = ">=3.10"
dependencies = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "python-dotenv",
    "httpx",
    "ghost-contracts"
]

[tool.setuptools.packages.find]
include = ["ghost*"]
```

**1.3 Design unified `ghost/app.py`**
```python
"""Unified Ghost platform FastAPI application."""

from contextlib import asynccontextmanager
from fastapi import FastAPI

from .control.api import create_app as create_control_app
from .obs.config import Settings as ObsSettings
# ... merge lifespan logic, route mounting, etc.

def create_app(
    control_settings=None,
    obs_settings=None,
    # ... combined parameters
) -> FastAPI:
    """Create unified Ghost application with control + obs routes."""
    
    # Option A: Mount as sub-apps (preserves existing route structure)
    # app.mount("/control", control_app)
    # app.mount("/obs", obs_app)
    
    # Option B: Merge routers (unified route space)
    # app.include_router(control_router, prefix="/api/v1")
    # app.include_router(obs_router, prefix="/api")
    
    # Option C: Keep separate FastAPI apps, unified lifespan
    # (recommended for minimal changes)
```

**Decision point**: Choose integration approach
- **Option A (Sub-apps)**: Minimal code changes, preserves all routes as-is
  - Control: `/control/openapi/v1/*`, `/control/api/v1/*`
  - Obs: `/obs/api/*`
  - Con: Awkward URLs

- **Option B (Merged routers)**: Clean unified API
  - Control: `/api/v1/*` (keep as-is)
  - Obs: `/api/*` (keep as-is, conflicts possible)
  - Con: Requires route prefix analysis

- **Option C (Separate apps + unified entry)**: Staged migration
  - Run both apps in same process
  - Keep separate route spaces initially
  - Merge incrementally
  - Con: Most complex

**Recommended: Option B with careful route analysis**

### Phase 2: Update Internal Imports

**2.1 Control module imports**
- Change `from .` to `from ghost.control.`
- Example: `from .config import Settings` → `from ghost.control.config import Settings`

**2.2 Obs module imports**
- Change `from .` to `from ghost.obs.`
- Example: `from .store import ObsStore` → `from ghost.obs.store import ObsStore`

**2.3 Worker imports (CRITICAL)**
- Update: `from obs.localserver import ...` → `from ghost.obs.localserver import ...`
- Files affected:
  - `packages/worker/ghost_worker/driver.py`
  - `packages/worker/ghost_worker/roster.py`

### Phase 3: Update Entry Points

**3.1 Update `main.py`**
```python
# Before
from ghost.api import create_app

# After
from ghost.app import create_app
# OR
from ghost import create_app
```

**3.2 Create unified Docker image**

**Dockerfile.ghost** (replaces Dockerfile.core + Dockerfile.platform):
```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8

WORKDIR /app

COPY packages/contracts /opt/packages/contracts
COPY packages/ghost /opt/packages/ghost
RUN pip install --no-cache-dir /opt/packages/contracts /opt/packages/ghost

EXPOSE 8000

# Unified entry point (both control + obs on same port)
CMD ["sh", "-c", "exec uvicorn ghost.app:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
```

**3.3 Update docker-compose.yaml**
```yaml
services:
  # Merged: control + platform → server
  server:
    build:
      context: .
      dockerfile: Dockerfile.ghost
    image: ghost-server:latest
    container_name: ghost-server
    restart: unless-stopped
    environment:
      # Merged control + obs env vars
      GHOST_TASKS_JSON: ${GHOST_TASKS_JSON:-}
      BENCHMARK_TOKEN: ${BENCHMARK_TOKEN:-}
      GHOST_ADMIN_TOKEN: ${GHOST_ADMIN_TOKEN:-}
      GHOST_WORKER_TOKEN: ${PLATFORM_WORKER_TOKEN:-}
      OBSERVABILITY_TOKEN: ${OBSERVABILITY_TOKEN:-}
      GHOST_DB_PATH: /app/data/ghost.sqlite3
      OBSERVABILITY_DB: /app/data/obs.sqlite3
      OBSERVABILITY_WEB: /app/web
    ports:
      - target: 8000
        host_ip: ${SERVER_HOST_IP:-127.0.0.0}
        published: ${SERVER_PORT:-8000}
    volumes:
      - ./data:/app/data
      - ./web:/app/web:ro
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"]
      
  worker:
    # ... unchanged, but update URLs
    environment:
      PLATFORM_URL: ${PLATFORM_URL:-http://server:8000}
      OBSERVABILITY_URL: ${OBSERVABILITY_URL:-http://server:8000}
    depends_on:
      server:
        condition: service_healthy
```

### Phase 4: Update Tests

**4.1 Move test files**
```bash
packages/ghost/tests/control/  # From packages/core/tests/
packages/ghost/tests/obs/      # From packages/obs/tests/
```

**4.2 Update test imports**
- Control tests: `from ghost.control import ...`
- Obs tests: `from ghost.obs import ...`

**4.3 Update pytest configuration**
```bash
# pytest.ini (if needed)
testpaths = packages/ghost/tests packages/worker/tests packages/contracts/tests
```

### Phase 5: Update Documentation

**5.1 CLAUDE.md**
- Update package structure section
- Update dependency flow diagram
- Update installation commands
- Update import examples

**5.2 README.md**
- Update architecture diagram (four packages → three packages)
- Update Docker commands
- Update local development instructions

**5.3 ARCHITECTURE.md**
- Update component descriptions
- Update data flow diagrams

### Phase 6: Clean Up

**6.1 Remove old packages**
```bash
rm -rf packages/core
rm -rf packages/obs
```

**6.2 Remove old Dockerfiles**
```bash
rm Dockerfile.core
rm Dockerfile.platform
```

**6.3 Update .gitignore** (if needed)

## Migration Checklist

### Pre-migration verification
- [ ] All tests pass: `pytest packages/core packages/obs`
- [ ] Docker images build successfully
- [ ] Docker compose stack runs successfully

### Create new structure
- [ ] Create `packages/ghost/` directory
- [ ] Create `packages/ghost/pyproject.toml`
- [ ] Create `packages/ghost/ghost/__init__.py`
- [ ] Copy `packages/core/ghost/*` → `packages/ghost/ghost/control/`
- [ ] Copy `packages/obs/obs/*` → `packages/ghost/ghost/obs/`
- [ ] Create `packages/ghost/ghost/app.py` (unified entry)
- [ ] Copy tests to `packages/ghost/tests/{control,obs}/`

### Update imports
- [ ] Update internal imports in `ghost/control/` modules
- [ ] Update internal imports in `ghost/obs/` modules
- [ ] Update worker imports: `obs.localserver` → `ghost.obs.localserver`
- [ ] Update test imports in control tests
- [ ] Update test imports in obs tests

### Update entry points
- [ ] Update `main.py` to use `ghost.app:create_app`
- [ ] Create `Dockerfile.ghost`
- [ ] Update `docker-compose.yaml` (merge control + platform → server)
- [ ] Update environment variable references in compose

### Verify
- [ ] Install new package: `pip install -e packages/ghost`
- [ ] Run tests: `pytest packages/ghost/tests/`
- [ ] Build Docker image: `docker build -f Dockerfile.ghost -t ghost-server:latest .`
- [ ] Test locally: `python main.py`
- [ ] Test with compose: `docker compose up --build`
- [ ] Verify worker can import: `python -c "from ghost.obs.localserver import serve_forever_in_thread"`

### Documentation
- [ ] Update CLAUDE.md
- [ ] Update README.md
- [ ] Update ARCHITECTURE.md
- [ ] Update any other references to packages/core or packages/obs

### Cleanup
- [ ] Remove `packages/core/`
- [ ] Remove `packages/obs/`
- [ ] Remove `Dockerfile.core`
- [ ] Remove `Dockerfile.platform`
- [ ] Commit staged changes

## Risk Mitigation

### Database Separation Maintained
- Keep separate SQLite files: `ghost.sqlite3` (control) and `obs.sqlite3` (obs)
- No schema changes required
- Both use WAL mode with single-writer constraint
- Future: Could merge into single DB, but not required for this refactor

### Route Conflicts
- Analyze all routes before merging:
  - Control: `/openapi/v1/*`, `/api/v1/*`
  - Obs: `/api/*` (reads), `/api/internal/*` (ingest)
- Potential conflict: `/api/v1/evaluations` (control) vs `/api/runs` (obs)
- Resolution: Different prefixes, no actual conflicts

### Worker Compatibility
- Worker MUST be able to import `ghost.obs.localserver`
- Verify import before finalizing
- Update worker editable install in compose: `-e packages/ghost`

### Rollback Plan
- Keep git commit history clean
- Single PR/commit for atomic merge
- Tag before merge: `git tag pre-merge-v0.1.0`
- If issues: `git revert` or `git reset --hard pre-merge-v0.1.0`

## Open Questions

1. **Port selection**: Single port (8000) or keep separate ports (8000 control, 8090 obs)?
   - **Recommendation**: Single port 8000, simplifies deployment
   - Obs routes won't conflict with control routes
   - Update worker env: `OBSERVABILITY_URL=http://server:8000`

2. **Database merge**: Keep separate DBs or merge into one?
   - **Recommendation**: Keep separate initially (safer migration)
   - Tables don't overlap, no immediate benefit to merging
   - Can merge later as optimization if needed

3. **Package name**: `ghost` or `ghost-platform` or `ghost-server`?
   - **Recommendation**: `ghost` (simple, matches module name)
   - PyPI name: `ghost` (or `ghost-platform` if namespace taken)

4. **Backward compatibility**: Should we keep `ghost-core` and `ghost-obs` as empty shims?
   - **Recommendation**: No, clean break
   - Document migration in changelog
   - Internal project, not public API

## Success Criteria

- [ ] All tests pass with new structure
- [ ] Docker compose stack runs successfully
- [ ] Worker can solve challenges in assignment mode
- [ ] Observability dashboard receives telemetry
- [ ] Canonical events flow from control to obs
- [ ] VPN endpoints work correctly
- [ ] No import errors in any module
- [ ] Documentation reflects new structure
