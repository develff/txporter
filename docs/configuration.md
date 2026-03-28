# Configuration

## banks.json

Location: `config/banks.json` (gitignored — never commit this file)

### Account types

#### FinTS (German banks)

```json
{
  "id": "dkb",
  "name": "DKB",
  "type": "fints",
  "aqbanking_id": 4,
  "blz": "12030000",
  "url": "https://fints.dkb.de/fints",
  "login": "your-login",
  "hbci_version": 300,
  "tan_mode": 7940
}
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier, used in API calls (e.g. `/sync/dkb`) |
| `aqbanking_id` | AqBanking UniqueId from `aqhbci-tool4 listaccounts -v` |
| `blz` | German bank code (Bankleitzahl) |
| `url` | FinTS server URL |
| `login` | Your online banking login |
| `tan_mode` | TAN method ID (see `aqhbci-tool4 listitanmodes`) |

#### PayPal

```json
{
  "id": "paypal",
  "name": "PayPal",
  "type": "paypal",
  "login": "your@email.com"
}
```

### Known FinTS URLs

| Bank | BLZ | URL | TAN mode |
|------|-----|-----|----------|
| DKB | 12030000 | `https://fints.dkb.de/fints` | 7940 (DKB App) |
| 1822direkt | 50050222 | `https://fints.1822direkt.com/fints/hbci` | 6903 (1822TAN+) |
| Consorsbank | 76030080 | `https://fin.consorsbank.de/auth` | pushTAN (unverified) |

### Targets

#### Firefly III

```json
"firefly": {
  "enabled": true,
  "url": "http://your-firefly-host:8080",
  "token": "your-personal-access-token"
}
```

#### CSV

```json
"csv": {
  "enabled": true,
  "path": "/home/txporter/output"
}
```

## PIN file

Location: `config/pinfile` (gitignored — never commit this file)

AqBanking reads PINs non-interactively from a PIN file. Generate it with:

```bash
docker compose run --rm txporter aqhbci-tool4 mkpinlist -o /home/txporter/config/pinfile
```

Then edit `config/pinfile` and add your PIN for each bank account in the format:

```
PIN_BLZ_LOGIN = "yourpin"
```

Protect the file: `chmod 600 config/pinfile`

The PIN file path can be overridden with the `TXPORTER_PINFILE` environment variable.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TXPORTER_CONFIG` | `/home/txporter/config/banks.json` | Path to config file |
| `TXPORTER_PINFILE` | `/home/txporter/config/pinfile` | Path to AqBanking PIN file |

## REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/accounts` | GET | List configured accounts |
| `/sync` | POST | Sync all accounts |
| `/sync/{id}` | POST | Start sync for one account (FinTS: returns `pending`, confirm in app) |
| `/sync/{id}/confirm` | POST | Complete a pending FinTS sync after TAN confirmation |
| `/status` | GET | Last sync status |
