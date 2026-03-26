# txporter

A Dockerized REST service that fetches transactions from financial accounts and forwards them to configurable targets.

## Features

- Fetch transactions from banks via FinTS/HBCI (using AqBanking)
- Fetch transactions from PayPal (using AqBanking)
- Forward transactions to Firefly III via REST API
- Forward transactions as CSV
- On-demand sync via REST API
- Multi-account support

## Supported Sources

| Source | Protocol | Status |
|--------|----------|--------|
| German banks (DKB, 1822direkt, Consorsbank, ...) | FinTS/HBCI | planned |
| PayPal | AqBanking PayPal backend | planned |

## Supported Targets

| Target | Status |
|--------|--------|
| Firefly III (REST API) | planned |
| CSV file | planned |

## Requirements

- Docker
- Docker Compose

## Quick Start

```bash
# Clone the repository
git clone https://github.com/develff/txporter.git
cd txporter

# Copy and edit config
cp config/banks.example.json config/banks.json
# Edit config/banks.json with your account details

# Initial bank setup (interactive, one-time)
docker compose run --rm txporter bash scripts/setup.sh

# Start the service
docker compose up -d

# Trigger a sync
curl -X POST http://localhost:8090/sync
```

## Architecture

```
Financial Account          txporter                    Target
─────────────────          ────────────────────        ──────────────
Bank (FinTS)    ──────────► AqBanking CLI              Firefly III API
PayPal          ──────────► │                      ──► CSV File
                            │                      │
                            REST API (/sync)  ──────┘
```

## Configuration

See [docs/configuration.md](docs/configuration.md) for details.

## Setup

See [docs/setup.md](docs/setup.md) for initial bank account setup.

## License

txporter is available under a dual license:

- **Open source use**: [GNU Affero General Public License v3.0](LICENSE)
  Free for personal and open source projects. Any modifications or
  hosted versions must be released under the same license.

- **Commercial use**: A separate commercial license is required.
  See [LICENSE.commercial](LICENSE.commercial) or contact the author.

Copyright (C) 2026 develff
