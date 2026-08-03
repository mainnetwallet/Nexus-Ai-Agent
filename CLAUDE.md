# CLAUDE.md — AI Assistant Guidelines for Nexus-Ai-Agent

This file defines how any AI assistant (Claude or otherwise) must operate when working
in this repository. These rules are permanent for this project unless explicitly
overridden by the repository owner in a future request.

## Project Status

Nexus-Ai-Agent is **feature-complete for v1.0**. This is the permanent codebase baseline —
treat existing architecture as stable and settled, not as a draft.

Stack: FastAPI backend, Next.js frontend, PostgreSQL, Redis, ChromaDB, JWT auth,
Playwright browser automation, plugin system.

## Repository Development Rules

- **Do not redesign the architecture.**
- **Do not create new core modules** unless explicitly requested by the owner.
- **Extend existing modules** (`backend/api`, `backend/browser`, `backend/config`,
  `backend/database`, `backend/memory`, `backend/planner`, `backend/plugins`,
  `backend/reports`, `backend/telegram`, `backend/vision`, `backend/wallet`,
  `frontend/src`) rather than writing parallel implementations.
- **Reuse existing code** — check for an existing utility/service before adding a new one.
- **Maintain backward compatibility** for APIs, plugin interfaces, and DB schemas.
- **Keep the project production-ready** at all times — every change should leave the
  repo in a deployable state.

## Coding Standards

- Python: PEP 8, type hints required, formatted with `black`, linted with `ruff`,
  type-checked with `mypy`.
- TypeScript/Next.js: follow existing ESLint/oxlint rules, typed components, no `any`
  without justification.
- No dead code, no commented-out blocks, no unused imports.

## Architecture Rules

- New capabilities → `backend/plugins`, not core routers or `main.py`.
- DB access → only through `backend/database`.
- Vector memory/embeddings → only through `backend/memory` (ChromaDB).
- Browser automation → only through `backend/browser` (Playwright).
- Wallet/blockchain logic → only through `backend/wallet`.
- Auth → existing JWT flow only; no parallel auth mechanisms.

## Testing Requirements

- Every task must end with running the test suite (`pytest`, configured via `pytest.ini`)
  and the frontend checks.
- All errors surfaced by tests, linters, or type checkers must be fixed before considering
  a task complete.
- New features and bug fixes require accompanying tests.

## Documentation Requirements

- Update `README.md`, `CHANGELOG.md`, and `docs/` whenever behavior, setup, or environment
  variables change.
- Update `RELEASE_NOTES.md` when a release is prepared.
- Keep this file and `CONTRIBUTING.md` as the source of truth for workflow — update them
  only when the owner explicitly changes the rules.

## Git Commit Conventions

Conventional Commits format: `type(scope): summary` — types: `feat`, `fix`, `refactor`,
`docs`, `test`, `chore`, `perf`, `ci`, `build`.

## Pull Request Checklist

- [ ] No unrequested architecture changes
- [ ] Existing modules reused
- [ ] Backward compatibility preserved
- [ ] All tests pass
- [ ] Linting/formatting clean
- [ ] Documentation updated
- [ ] No secrets/tokens committed
- [ ] Conventional commit messages

## Feature Development Workflow

1. Fit the feature into existing architecture.
2. Implement inside the relevant existing module.
3. Add/update tests.
4. Update documentation.
5. Run full test suite and linters.
6. Follow the PR checklist.

## Bug Fix Workflow

1. Reproduce with a failing test.
2. Apply the minimal fix in the existing module.
3. Confirm the fix and that no other tests break.
4. Update `CHANGELOG.md`.
5. Follow the PR checklist.

## Release Workflow

1. Confirm `main` is green.
2. Update `CHANGELOG.md` and `RELEASE_NOTES.md`.
3. Bump version references.
4. Tag release using semantic versioning (`vX.Y.Z`).
5. Confirm deployment config (`docker-compose.yml`, `docker/`) is consistent with the release.
6. Publish release notes.

## Standing Instructions for AI Assistants

- Do not ask for confirmation on these rules again — they are the standing guidelines
  for this repository.
- For every task: extend existing code, keep backward compatibility, run tests, fix all
  resulting errors, and update documentation before considering the task done.
