#!/bin/bash
# setup.sh — DEPRECATED
#
# Bank setup is now done via the REST API. Use the following flow instead:
#
#   Step 1 — Register bank (profile-based):
#     curl -X POST http://localhost:8090/setup \
#       -H "Content-Type: application/json" \
#       -d '{"bank": "dkb", "login": "YOUR_LOGIN", "pin": "YOUR_PIN"}'
#
#   Step 2 — Set TAN mode (only needed if not auto-selected in step 1):
#     curl -X POST http://localhost:8090/setup/{setup_id}/tanmode \
#       -H "Content-Type: application/json" \
#       -d '{"tan_mode": 7940}'
#
#   Step 3 — Confirm TAN (after confirming in your banking app):
#     curl -X POST http://localhost:8090/setup/{setup_id}/confirm
#
#   List available bank profiles:
#     curl http://localhost:8090/setup/profiles
#
#   List registered accounts:
#     curl http://localhost:8090/accounts
#
# See docs/setup.md for the full guide.

echo "ERROR: setup.sh is deprecated." >&2
echo "       Bank setup is now done via the txporter REST API." >&2
echo "       See docs/setup.md or run: curl http://localhost:8090/setup/profiles" >&2
exit 1
