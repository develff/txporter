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
| `/tags` | GET | List all tag names from Firefly III |
| `/csv/fields` | GET | List all Firefly fields available for CSV column mapping |
| `/csv/preview` | POST | Upload a CSV file and return headers + first 5 rows (form: `file`, `delimiter`, `encoding`, `skip_rows`) |
| `/csv/import` | POST | Import a CSV file using a mapping profile (form: `file`, `mapping` as JSON string) |
| `/csv/mappings` | GET | List all saved CSV mapping profiles |
| `/csv/mappings` | POST | Save or update a CSV mapping profile (body: JSON with `id` and `name`) |
| `/csv/mappings/{id}` | DELETE | Delete a CSV mapping profile by id |

## CSV Import

CSV import allows importing transactions from any CSV file (e.g. Crypto.com, PayPal export) into Firefly III without a FinTS connection.

### Mapping profiles

A mapping profile defines how CSV columns map to Firefly III transaction fields. Profiles are saved in `config/csv_mappings.json`.

```json
{
  "id": "crypto-com-visa",
  "name": "Crypto.com Visa",
  "delimiter": ",",
  "encoding": "utf-8",
  "skip_rows": 0,
  "account_name": "Crypto.com Visa",
  "fields": {
    "date":                  { "column": "Timestamp (UTC)", "date_format": "%Y-%m-%d %H:%M:%S" },
    "amount":                { "column": "Amount" },
    "currency_code":         { "column": "Currency" },
    "description":           { "column": "Transaction Description" },
    "foreign_amount":        { "column": "To Amount" },
    "foreign_currency_code": { "column": "To Currency" },
    "tags":                  { "value": "CDC-CSV" }
  }
}
```

Each field can be mapped either from a CSV column (`"column": "ColName"`) or set to a fixed value (`"value": "..."`) that applies to every row.

Required fields: `date`, `amount`, `currency_code`, `description`.

### Date format

Use Python `strptime` format strings, e.g. `%Y-%m-%d %H:%M:%S` for `2024-01-15 09:23:41`.

### Amount format

For European number formats set `decimal_sep` and `thousands_sep`:

```json
"amount": { "column": "Betrag", "decimal_sep": ",", "thousands_sep": "." }
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TXPORTER_CSV_MAPPINGS` | `<config_dir>/csv_mappings.json` | Path to CSV mapping profiles file |
