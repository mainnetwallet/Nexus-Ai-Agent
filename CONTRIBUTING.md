# Contributing to Nexus-Ai-Agent

This repository is **feature-complete for v1.0**. All contributions must extend the
existing system without altering its core architecture.

## 1. Repository Development Rules

- Do **not** redesign the architecture (FastAPI backend, Next.js frontend,
  PostgreSQL, Redis, ChromaDB, JWT auth, Playwright browser automation, plugin system).
- Do **not** create new core modules (`backend/api`, `backend/browser`, `backend/config`,
  `backend/database`, `backend/memory`, `backend/planner`, `backend/plugins`,
  `backend/reports`, `backend/telegram`, `backend/vision`, `backend/wallet`)
  unless explicitly requested.
- Always extend and reuse existing modules, services, and utilities before writing new ones.
- Maintain backward compatibility for all public APIs, plugin interfaces, and DB schemas.
- Keep the project production-ready at every commit — no partially working states on `main`.

## 2. Coding Standards

- **Backend (Python):** follow PEP 8, type-hint all functions, run `black`, `ruff`, and `mypy`
  before committing. Keep FastAPI route handlers thin — business logic belongs in service/module files.
- **Frontend (TypeScript/Next.js):** follow the existing ESLint/oxlint config, prefer functional
  components and hooks, keep components typed (no `any` unless justified with a comment).
- No unused imports, dead code, or commented-out blocks left in commits.
- Reuse existing config patterns (`backend/config`) instead of hardcoding values.

## 3. Architecture Rules

- Plugin system: new capabilities go through `backend/plugins`, not into `main.py` or core routers.
- Database access goes through `backend/database` — no ad-hoc DB connections elsewhere.
- Memory/vector store operations go through `backend/memory` (ChromaDB) — do not bypass it.
- Browser automation stays inside `backend/browser` (Playwright) — do not spawn ad-hoc browser instances.
- Wallet/blockchain logic stays inside `backend/wallet`.
- Auth stays JWT-based via existing auth utilities — do not introduce a second auth mechanism.

## 4. Testing Requirements

- All existing tests in `backend/tests` (run via `pytest.ini`) must pass before merge.
- Every new feature or bug fix must include or update relevant tests.
- Run the full suite locally: `pytest` (backend) and the frontend test/lint scripts before pushing.
- Do not merge with failing or skipped tests without an explicit, documented reason.

## 5. Documentation Requirements

- Update `README.md` if setup, usage, or environment variables change.
- Update `CHANGELOG.md` for every user-facing change.
- Update `RELEASE_NOTES.md` when preparing a release.
- Update relevant files in `docs/` when architecture-adjacent behavior changes.
- Update this file and `CLAUDE.md` only when development rules themselves change.

## 6. Git Commit Conventions

Use Conventional Commits:

```
<type>(<scope>): <short summary>

[optional body]
[optional footer]
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`, `build`.

Examples:
- `feat(plugins): add price-alert plugin`
- `fix(wallet): correct gas estimation rounding`
- `docs(readme): update env variable list`

## 7. Pull Request Checklist

Before opening a PR, confirm:

- [ ] No architecture changes unless explicitly approved.
- [ ] Existing modules reused where possible; no unnecessary new modules.
- [ ] Backward compatibility preserved (API, plugin interface, DB schema).
- [ ] All tests pass (`pytest`, frontend checks).
- [ ] Linting/formatting clean (`black`, `ruff`, `mypy`, ESLint/oxlint).
- [ ] Documentation updated (`README.md`, `CHANGELOG.md`, `docs/` as needed).
- [ ] No secrets, tokens, or credentials committed.
- [ ] Commit messages follow Conventional Commits.

## 8. Feature Development Workflow

1. Confirm the feature fits within existing architecture; extend, don't redesign.
2. Branch from `main`: `feat/<short-description>`.
3. Implement inside the relevant existing module.
4. Add/update tests covering the new behavior.
5. Update documentation.
6. Run full test suite and linters locally.
7. Open PR using the checklist above.

## 9. Bug Fix Workflow

1. Reproduce the bug and add a failing test that captures it.
2. Branch from `main`: `fix/<short-description>`.
3. Apply the minimal fix within the existing module — no unrelated refactors.
4. Confirm the new test passes and no existing tests break.
5. Update `CHANGELOG.md`.
6. Open PR using the checklist above.

## 10. Release Workflow

1. Ensure `main` is green (all tests passing, lint clean).
2. Update `CHANGELOG.md` and `RELEASE_NOTES.md` with the release summary.
3. Bump version references where applicable.
4. Tag the release (`vX.Y.Z`) following semantic versioning.
5. Confirm production deployment config (`docker-compose.yml`, `docker/`) is unaffected
   or updated consistently with the release.
6. Publish release notes.
