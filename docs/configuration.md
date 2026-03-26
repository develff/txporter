# Configuration

## banks.json

Location: `config/banks.json` (gitignored — never commit this file)

### Account types

#### FinTS (German banks)

```json
{
  "id": "unique-id",
  "name": "Display name",
  "type": "fints",
  "blz": "12030000",
  "url": "https://fints.dkb.de/fints",
  "login": "your-login",
  "hbci_version": 300,
  "tan_mode": 7940
}
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier, used in API calls |
| `blz` | German bank code (Bankleitzahl) |
| `url` | FinTS server URL |
| `login` | Your online banking login |
| `tan_mode` | TAN method ID (see bank documentation) |

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

| Bank | BLZ | URL |
|------|-----|-----|
| DKB | 12030000 | `https://fints.dkb.de/fints` |
| 1822direkt | 50050201 | `https://banking.1822direkt.com/banking/FinTS` |
| Consorsbank | 76030080 | `https://fin.consorsbank.de/auth` |

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

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TXPORTER_CONFIG` | `/home/txporter/config/banks.json` | Path to config file |

## REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/accounts` | GET | List configured accounts |
| `/sync` | POST | Sync all accounts |
| `/sync/{id}` | POST | Sync single account |
| `/status` | GET | Last sync status |
