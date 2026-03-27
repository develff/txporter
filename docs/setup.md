# Setup Guide

## Docker base image

txporter uses **openSUSE Tumbleweed** (`opensuse/tumbleweed`) as its base image.

Rationale (evaluated 2026-03-26):

| Image | AqBanking | Base size | Rolling | glibc |
|-------|-----------|-----------|---------|-------|
| openSUSE Tumbleweed | 6.9.1 | 148 MB | yes | yes |
| Arch Linux | 6.9.1 | 558 MB | yes | yes |
| Ubuntu 25.10 | 6.6.0 | 135 MB | no | yes |

openSUSE Tumbleweed ships the same AqBanking version as Arch Linux (6.9.1) but
with a significantly smaller base image. Ubuntu 25.10 is three minor versions
behind and is not a rolling release. Alpine Linux was ruled out due to musl libc
compatibility risks with AqBanking's FinTS/TLS stack.

Both `aqbanking-cli` and `aqhbci-tool4` are present and functional in the built
image.

## Prerequisites

- Docker and Docker Compose installed
- Bank account credentials (login, PIN)
- Firefly III instance running (optional)

## Initial Setup

### 1. Clone and configure

```bash
git clone https://github.com/develff/txporter.git
cd txporter
cp config/banks.example.json config/banks.json
chmod 600 config/banks.json
```

Edit `config/banks.json` to add your Firefly III target config.
Bank accounts are registered via the REST API (see below) — no manual editing needed.

### 2. Start the service

```bash
docker compose up -d
```

### 3. Register a bank account (REST API)

Bank registration is done via the txporter REST API. No manual file editing or
shell access is required.

#### List available predefined profiles

```bash
curl http://localhost:8090/setup/profiles
```

Currently defined profiles: **dkb**, **1822direkt**.
Unknown banks can be registered manually by supplying all fields (see below).

#### Step 1 — Register bank

```bash
# Profile-based (recommended):
curl -X POST http://localhost:8090/setup \
  -H "Content-Type: application/json" \
  -d '{"bank": "dkb", "login": "YOUR_LOGIN", "pin": "YOUR_PIN"}'
```

```bash
# Manual (no predefined profile):
curl -X POST http://localhost:8090/setup \
  -H "Content-Type: application/json" \
  -d '{
    "blz": "12030000",
    "url": "https://fints.dkb.de/fints",
    "login": "YOUR_LOGIN",
    "pin": "YOUR_PIN",
    "name": "DKB Girokonto",
    "hbci_version": 300,
    "tan_mode": 7940
  }'
```

Profile fields (`blz`, `url`, `hbci_version`, `tan_mode`) can be overridden per-request.

**Response (profile with known TAN mode — TAN mode auto-selected):**
```json
{
  "setup_id": "uuid-...",
  "status": "pending_confirm",
  "message": "Confirm TAN in banking app, then POST /setup/{id}/confirm",
  "tan_modes": [...],
  "auto_selected_tan_mode": 7940
}
```

Confirm the TAN in your banking app, then proceed to Step 3.

**Response (no TAN mode known — manual selection required):**
```json
{
  "setup_id": "uuid-...",
  "status": "pending_tan_mode",
  "message": "Select TAN mode, then POST /setup/{id}/tanmode",
  "tan_modes": [{"id": 7940, "description": "DKB App (pushTAN)"}, ...]
}
```

Proceed to Step 2.

#### Step 2 — Set TAN mode (only needed if not auto-selected)

```bash
curl -X POST http://localhost:8090/setup/{setup_id}/tanmode \
  -H "Content-Type: application/json" \
  -d '{"tan_mode": 7940}'
```

This triggers a TAN request in your banking app.
Confirm it, then proceed to Step 3.

#### Step 3 — Confirm

```bash
curl -X POST http://localhost:8090/setup/{setup_id}/confirm
```

**Response:**
```json
{
  "status": "ok",
  "account_id": "dkb",
  "aqbanking_id": 1,
  "iban": "DE12300120001234567890",
  "accounts": [...]
}
```

The account is now fully registered. `aqbanking_id` and `iban` are written back to
`config/banks.json` automatically.

### 4. Verify

```bash
# List all registered accounts
curl http://localhost:8090/accounts

# Trigger a sync
curl -X POST http://localhost:8090/sync/dkb
# → {"status":"pending","message":"Confirm in banking app, then POST /sync/dkb/confirm"}

curl -X POST http://localhost:8090/sync/dkb/confirm
# → {"status":"ok","transactions":N}
```

Banks with read-only access (e.g. 1822direkt since 2025-09-16) do not require TAN
confirmation — `/sync/{id}` returns `{"status":"ok"}` directly.

### Account management

```bash
# List all accounts (with aqbanking_id and IBAN after setup)
curl http://localhost:8090/accounts

# Remove an account registration
curl -X DELETE http://localhost:8090/accounts/{aqbanking_id}
```
