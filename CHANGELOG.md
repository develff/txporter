# Changelog

## [1.1.0] - 2026-05-15

### Fixed
- Fresh Docker deployment now works without manual setup: volumes are auto-created, config and output directories are owned correctly on first start
- Deduplication now uses the global Firefly transactions endpoint, catching transactions reclassified to other accounts (e.g. PayPal-Transit)
- Firefly asset account is matched by IBAN or account number, not just name — renames no longer cause duplicates

### Changed
- Firefly and CSV targets are now active as soon as they are configured (URL+token or path) — the enabled/disabled toggle has been removed
- Import report CSV is written to the output directory after every Firefly sync and available as a download link in the UI
- "Export as CSV" and import report now contain the same fields (date, amount, currency, description, remote name/IBAN/account, external ID, end-to-end reference, primanota, category, budget, tags, foreign amount)

### Added
- "Open Firefly →" link in the header, visible once a Firefly URL is configured
- Optional browser URL override in Firefly settings (Advanced) for SSO / reverse-proxy setups where the API URL is not reachable from the browser
- Docker image published to ghcr.io on every develop push and release tag

## [1.0.0] - 2026-05-14

Initial release.

- Fetch transactions from German banks via FinTS/HBCI (DKB, 1822direkt, Consorsbank)
- Import transactions from CSV exports
- Forward to Firefly III via REST API or CSV file
- Web UI for bank account setup, sync, CSV import wizard, and settings
- Scheduled automatic sync
- Multi-account support
