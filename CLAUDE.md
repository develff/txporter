# txporter — Claude Code Context

## What is this project?

txporter is a Dockerized REST service that fetches transactions from financial
accounts (banks via FinTS/HBCI, PayPal) and forwards them to configurable targets
(Firefly III REST API, CSV files).

It uses AqBanking CLI as the underlying library for bank communication.

## Why does this exist?

There is no self-hosted, privacy-first, multi-account transaction fetcher that:
- Runs entirely in Docker (no local dependencies)
- Exposes a REST API for on-demand sync
- Supports multiple German banks + PayPal via a single config file
- Writes directly to Firefly III without a CSV detour

## Architecture

```
Financial Account     txporter              Target
─────────────────     ────────────────────  ──────────────
Bank (FinTS)    ────► AqBanking CLI     ──► Firefly III API
PayPal          ────► │                ──► CSV File
                      REST API (/sync)
```

## Key decisions

- **AqBanking** over php-fints: more mature, no browser redirect issues with pushTAN
- **On-demand sync** (not cron): PSD2 requires TAN confirmation per sync session,
  user triggers manually and confirms in banking app
- **Python/Flask** for REST API: simple, readable, good subprocess support
- **AGPL-3.0 + commercial license**: open source but no commercial use without permission
- **Docker base image**: TBD — prefer rolling release (Arch, openSUSE Tumbleweed)
  over Debian/Ubuntu LTS for more current AqBanking packages

## Current state

- [x] Project structure created
- [x] README, LICENSE, docker-compose.yml, Dockerfile (skeleton)
- [x] src/server.py — Flask REST API skeleton
- [x] src/aqbanking.py — AqBanking CLI wrapper skeleton
- [x] src/firefly.py — Firefly III API client skeleton
- [x] config/banks.example.json — example configuration
- [x] docs/setup.md, docs/configuration.md
- [x] Dockerfile: openSUSE Tumbleweed (AqBanking 6.9.1, 148 MB base, rolling, glibc)
- [x] scripts/setup.sh: interactive AqBanking bank setup script
- [x] src/aqbanking.py: implement CTX output parsing (all fields, external_id, tests)
- [x] src/firefly.py: implement proper transaction mapping to Firefly III API (storeTransaction)
- [x] First working Docker build
- [x] First real DKB sync test (used to gather sample data for field mapping)

## Known FinTS details

| Bank | BLZ | URL | TAN mode |
|------|-----|-----|----------|
| DKB | 12030000 | https://fints.dkb.de/fints | 7940 (DKB App) |
| 1822direkt | 50050222 | https://fints.1822direkt.com/fints/hbci | 6903 (1822TAN+, HKTAN V6/PSD2, read-only since 2025-09-16) |
| Consorsbank | 76030080 | https://fin.consorsbank.de/auth | pushTAN (URL unverified) |

## AqBanking setup flow (per bank, one-time interactive)

```bash
aqhbci-tool4 adduser -t pintan --context=1 -b BLZ -u LOGIN -s URL -N "Name" --hbciversion=300
aqhbci-tool4 getsysid -u 1        # triggers TAN in app
aqhbci-tool4 listitanmodes -u 1
aqhbci-tool4 setitanmode -u 1 -m TAN_MODE
aqhbci-tool4 getaccounts -u 1     # triggers TAN in app
aqhbci-tool4 listaccounts -v
aqhbci-tool4 getaccsepa -a ACCOUNT_ID
```

## Firefly III integration

- txporter writes directly to Firefly III REST API
- API token stored in config/banks.json (gitignored)
- Firefly III URL configured in banks.json targets section

## Next steps (priority order)

1. ~~Choose and test Docker base image~~ — done (openSUSE Tumbleweed)
2. ~~Get Docker build working~~ — Dockerfile in place
3. ~~Implement scripts/setup.sh for interactive bank registration~~ — done
4. ~~Implement aqbanking.py CTX output parsing~~ — done (issue #5)
5. ~~Implement firefly.py transaction mapping to Firefly III storeTransaction API~~ — done (issue #7)
6. ~~End-to-end test: DKB → txporter → Firefly III~~ — done, amounts and deposit/withdrawal mapping correct

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

- SonarCloud: decision pending (depends on repo visibility)
- Will be configured when repo goes public
