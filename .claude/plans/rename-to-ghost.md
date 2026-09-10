# Plan: Rename TsecBench to Ghost

## Overview
Rename the entire project from "TsecBench" to "ghost" across all files, packages, and configurations. This involves:
- 4 Python packages to rename
- 600+ occurrences across the codebase
- Directory structure changes
- Docker images and container names
- Import statements throughout the codebase
- Documentation and configuration files

## Naming Strategy
Based on the analysis, I'll use these replacements:
- `TsecBench` / `TSecBench` → `Ghost`
- `tsecbench` → `ghost`
- `tsecbench-` (in package names) → `ghost-`
- `tsecbench_` (in module names) → `ghost_`
- `TSECBENCH_` (env vars) → `GHOST_`

## Phase 1: Python Package Renaming

### 1.1 Rename Package Directories
- `packages/contracts/tsecbench_contracts/` → `packages/contracts/ghost_contracts/`
- `packages/core/tsecbench/` → `packages/core/ghost/`
- `packages/obs/obs/` → keep as `obs/` (already distinct name)
- `packages/worker/tsecbench_worker/` → `packages/worker/ghost_worker/`

### 1.2 Update pyproject.toml Files
For each package, update:
- `name`: `tsecbench-*` → `ghost-*`
- `description`: Replace project name
- `dependencies`: Update cross-package references
- `include`: Update package patterns
- `[project.scripts]`: Update entry points

Files to modify:
- `packages/contracts/pyproject.toml`
- `packages/core/pyproject.toml`
- `packages/obs/pyproject.toml`
- `packages/worker/pyproject.toml`

## Phase 2: Import Statements
Update all Python imports:
- `import tsecbench` → `import ghost`
- `import tsecbench_contracts` → `import ghost_contracts`
- `import tsecbench_worker` → `import ghost_worker`
- `import tsecbench.` → `import ghost.`
- `from tsecbench` → `from ghost`

Affected directories:
- All Python files in `packages/core/`
- All Python files in `packages/worker/`
- All Python files in `packages/obs/`
- All test files

## Phase 3: Docker Infrastructure

### 3.1 Dockerfile.core
- Update comments mentioning "TsecBench"
- Update `TSECBENCH_DB_PATH` → `GHOST_DB_PATH`
- Update CMD to use `ghost.api:create_app`

### 3.2 Dockerfile (worker)
- Update all comments
- Update package paths
- Update `ADAPTER_WORKDIR` references if needed

### 3.3 Dockerfile.platform
- Update comments
- Update package references

### 3.4 docker-compose.yaml
- Service names: `tsecbench-control` → `ghost-control`, `tsecbench-worker` → `ghost-worker`, `tsecbench-platform` → `ghost-platform`
- Image names: `tsecbench-*` → `ghost-*`
- Container names: `tsecbench-*` → `ghost-*`
- All environment variables: `TSECBENCH_*` → `GHOST_*`
- Volume paths: `tsecbench.sqlite3` → `ghost.sqlite3`, `obs.sqlite3` stays as is
- Comments and descriptions

### 3.5 Dockerfile.base
- Update comments referencing project name
- Update image tag: `tsecbench/kali:latest` → `ghost/kali:latest`

## Phase 4: Configuration Files

### 4.1 Environment Variables
Update in:
- `.env.example`
- `docker-compose.yaml` (already covered)
- Documentation

Variables to rename:
- `TSECBENCH_CONFIG` → `GHOST_CONFIG`
- `TSECBENCH_TASKS_JSON` → `GHOST_TASKS_JSON`
- `TSECBENCH_ADMIN_TOKEN` → `GHOST_ADMIN_TOKEN`
- `TSECBENCH_WORKER_TOKEN` → `GHOST_WORKER_TOKEN`
- `TSECBENCH_PUBLIC_BASE_URL` → `GHOST_PUBLIC_BASE_URL`
- `TSECBENCH_DB_PATH` → `GHOST_DB_PATH`
- `TSECBENCH_MAX_ACTIVE_CHALLENGES` → `GHOST_MAX_ACTIVE_CHALLENGES`
- `TSECBENCH_PROVISIONER` → `GHOST_PROVISIONER`

### 4.2 entrypoint.sh
- Update environment variable references
- Update any path references

## Phase 5: Frontend Code

### 5.1 TypeScript/JavaScript Files
Files to check and update in `frontend/src/`:
- `lib/api.ts`
- `lib/types.ts`
- Any other files with API endpoints or references

### 5.2 Package Configuration
- `frontend/package.json` (if it contains project name)

## Phase 6: Documentation

### 6.1 README.md
- Update project title
- Update all package names in examples
- Update command examples with new env vars
- Update image names in docker commands
- Update service names
- Update Chinese text: "TSecBench(极简 MVP)" → "Ghost(极简 MVP)"

### 6.2 Other Documentation
- `CHALLENGES_API.md` (if exists)
- `SDK_API.md` (if exists)
- `CLAUDE.md` (if exists)
- Any other `.md` files

## Phase 7: Test Files
Update all test files in:
- `packages/contracts/tests/`
- `packages/core/tests/`
- `packages/obs/tests/`
- `packages/worker/tests/`

## Phase 8: Configuration and Build Files
- `pytest.ini` (if it contains project references)
- `.gitignore` (if it contains tsecbench-specific patterns)
- Any CI/CD configuration files

## Phase 9: Clean Up Generated Files
Delete egg-info directories (these will be regenerated):
- `packages/contracts/tsecbench_contracts.egg-info/`
- `packages/core/tsecbench_core.egg-info/`
- `packages/obs/tsecbench_obs.egg-info/`
- `packages/worker/tsecbench_worker.egg-info/`

## Phase 10: Verification
After all changes:
1. Search for any remaining "tsecbench" or "TsecBench" references
2. Verify Python imports work
3. Verify Docker builds succeed
4. Run tests if available

## Excluded from Renaming
- Keep `.venv/` as is (will be regenerated)
- Keep `data/` directory (runtime data)
- Keep `work/` directory (runtime data)
- Keep `.claude/worktrees/` (temporary)
- External references to official `tsec-benchmark` SDK package (this is a different project)

## Execution Order
1. Rename Python package directories
2. Update pyproject.toml files
3. Update all Python import statements
4. Update Docker files
5. Update docker-compose.yaml
6. Update configuration files and env vars
7. Update frontend code
8. Update documentation
9. Update test files
10. Clean up egg-info directories
11. Verify changes

## Risk Assessment
- **Medium Risk**: This is a comprehensive refactoring affecting all parts of the project
- **Reversible**: Yes, changes are tracked by git
- **Breaking Changes**: Yes, existing deployments will need to update environment variables and rebuild containers
- **Testing Required**: Full integration testing after changes

## Notes
- The official SDK `tsec-benchmark` is an external dependency and should NOT be renamed
- Database file paths will change, which may require data migration for existing deployments
- Environment variable changes will require updating deployment configurations
