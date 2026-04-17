# txporter — Claude Code Context

## What is this project?

txporter is a Dockerized REST service that fetches transactions from financial
accounts (banks via FinTS/HBCI) and forwards them to configurable targets
(Firefly III REST API, CSV files).

It uses AqBanking CLI as the underlying library for bank communication.

## Why does this exist?

There is no self-hosted, privacy-first, multi-account transaction fetcher that:
- Runs entirely in Docker (no local dependencies)
- Exposes a REST API for on-demand sync
- Supports multiple German banks via a single config file
- Writes directly to Firefly III without a CSV detour

## Architecture

```
Financial Account     txporter              Target
─────────────────     ────────────────────  ──────────────
Bank (FinTS)    ────► AqBanking CLI     ──► Firefly III API
CSV file        ────► CSV Import Wizard ──► CSV File
                      REST API + Web UI
```

## Key decisions

- **AqBanking** over php-fints: more mature, no browser redirect issues with pushTAN
- **On-demand sync** (not cron): PSD2 requires TAN confirmation per sync session,
  user triggers manually and confirms in banking app
- **Python/Flask** for REST API: simple, readable, good subprocess support
- **AGPL-3.0 + commercial license**: open source but no commercial use without permission
- **Docker base image**: openSUSE Tumbleweed — rolling release, current AqBanking packages

## Known FinTS details

| Bank | BLZ | URL | TAN mode |
|------|-----|-----|----------|
| DKB | 12030000 | https://fints.dkb.de/fints | 7940 (DKB App) |
| 1822direkt | 50050222 | https://fints.1822direkt.com/fints/hbci | 6903 (1822TAN+, HKTAN V6/PSD2) |
| Consorsbank | 76030080 | https://brokerage-hbci.consorsbank.de/hbci | pushTAN. Login = Kontonummer + 3-stellige Ber.-Nr., z.B. 900123456001 |

## AqBanking setup flow (automated via REST API)

Bank registration is driven by the REST API (`POST /setup`, `POST /setup/{id}/tanmode`,
`POST /setup/{id}/confirm`). See `docs/setup.md` for the full flow.

The underlying `aqhbci-tool4` commands executed per step:
1. `adduser` → `getsysid` → `listitanmodes` (→ `setitanmode` + start `getaccounts` if TAN mode known)
2. `setitanmode` + start `getaccounts` (triggers TAN in app)
3. wait for `getaccounts` → `getaccsepa` → `listaccounts` → write `aqbanking_id` to banks.json

## Firefly III integration

- txporter writes directly to Firefly III REST API
- API token stored in config/banks.json (gitignored)
- Firefly III URL configured in banks.json targets section

## Development Guidelines

### Branching strategy

- `main` — stable, released code only
- `develop` — integration branch, all features merge here first
- `feature/*` — one branch per feature/issue (e.g. `feature/dockerfile`, `feature/aqbanking-parser`)
- `fix/*` — for bug fixes

### Merging rules

- **All merges via PR** — no direct commits to `main` or `develop`
- Feature branches → `develop` via PR
- `develop` → `main` via PR (release)
- PR should reference the related GitHub Issue (e.g. `Closes #12`)

### Issues

- Use GitHub Issues for all tasks, bugs and features
- Before starting work: create or reference an issue
- Branch name should reference issue number where possible (e.g. `feature/12-dockerfile`)

### Testing

- Every new feature or bugfix must include tests
- Tests live in `tests/` mirroring the `src/` structure
- Run tests before creating a PR:
  ```bash
  python3 -m pytest tests/
  ```

### Code analysis

- SonarCloud integration is configured in `.github/workflows/sonar.yml`
- Quality Gate must pass before merging to `main`
