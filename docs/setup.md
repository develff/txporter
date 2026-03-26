# Setup Guide

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

Edit `config/banks.json` with your bank credentials.
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
aqhbci-tool4 listaccounts -v
```

Exit the container — AqBanking config is persisted in the Docker volume.

### 3. Start the service

```bash
docker compose up -d
```

### 4. Test

```bash
# Health check
curl http://localhost:8090/health

# List configured accounts
curl http://localhost:8090/accounts

# Trigger sync for DKB
curl -X POST http://localhost:8090/sync/dkb
```

When syncing, confirm the push notification in your banking app.
