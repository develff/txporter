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
image. Running `aqbanking-cli listtransactions` requires a configured bank account
(see initial bank registration below).

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
```

Edit `config/banks.json` with your bank credentials and `aqbanking_id` (see step 2).
**Keep this file secure — it contains sensitive data.**

```bash
chmod 600 config/banks.json
```

### 2. Initial bank registration (one-time, interactive)

This step registers your bank accounts with AqBanking.
You will need to confirm a TAN in your banking app.

```bash
docker compose run --rm txporter bash
```

Inside the container, for each bank:

```bash
# DKB example
aqhbci-tool4 adduser -t pintan --context=1 \
  -b 12030000 \
  -u YOUR_LOGIN \
  -s https://fints.dkb.de/fints \
  -N "Vorname Nachname" \
  --hbciversion=300

aqhbci-tool4 getsysid -u 1        # triggers TAN in app
aqhbci-tool4 listitanmodes -u 1
aqhbci-tool4 setitanmode -u 1 -m 7940  # DKB App TAN mode
aqhbci-tool4 getaccounts -u 1     # triggers TAN in app
aqhbci-tool4 listaccounts -v      # note the UniqueId for each account
aqhbci-tool4 getaccsepa -a ACCOUNT_ID
```

The `listaccounts -v` output shows a `UniqueId` for each account — set this as
`aqbanking_id` in `config/banks.json`.

Exit the container — AqBanking config is persisted in the Docker volume.

### 3. Set up PIN file

txporter supplies PINs to AqBanking non-interactively via a PIN file.
Generate the template:

```bash
docker compose run --rm txporter aqhbci-tool4 mkpinlist -o /home/txporter/config/pinfile
```

Edit `config/pinfile` on the host and add your PIN for each account:

```
PIN_BLZ_LOGIN = "yourpin"
```

Protect it: `chmod 600 config/pinfile`

### 4. Start the service

```bash
docker compose up -d
```

### 5. Test

```bash
# Health check
curl http://localhost:8090/health

# List configured accounts
curl http://localhost:8090/accounts

# Trigger sync for DKB (step 1: starts FinTS request)
curl -X POST http://localhost:8090/sync/dkb
# → {"status":"pending","message":"Confirm in banking app, then POST /sync/dkb/confirm"}

# Confirm in your banking app, then complete the sync (step 2)
curl -X POST http://localhost:8090/sync/dkb/confirm
# → {"status":"ok","transactions":N}
```

Banks with read-only access (e.g. 1822direkt since 2025-09-16) do not require TAN
confirmation — `/sync/{id}` returns `{"status":"ok"}` directly.
