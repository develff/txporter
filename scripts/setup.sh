#!/bin/bash
# setup.sh — interactive AqBanking bank setup
# Reads bank accounts from config/banks.json and guides the user through
# the one-time aqhbci-tool4 registration flow for each FinTS account.
#
# Usage: setup.sh [--config PATH]
#   --config PATH   Path to banks.json (default: config/banks.json)

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${REPO_ROOT}/config/banks.json"

# ── Argument parsing ───────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_FILE="$2"
      shift 2
      ;;
    --config=*)
      CONFIG_FILE="${1#*=}"
      shift
      ;;
    -h|--help)
      sed -n '/^# Usage:/,/^[^#]/{ /^[^#]/d; s/^# \?//p }' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# ── Dependency checks ──────────────────────────────────────────────────────────

if ! command -v jq &>/dev/null; then
  echo "ERROR: 'jq' is required but not installed." >&2
  echo "       Install it with your package manager (e.g. zypper install jq)." >&2
  exit 1
fi

if ! command -v aqhbci-tool4 &>/dev/null; then
  echo "ERROR: 'aqhbci-tool4' is required but not found in PATH." >&2
  echo "       Run this script inside the txporter Docker container or install AqBanking." >&2
  exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: Config file not found: $CONFIG_FILE" >&2
  echo "       Copy config/banks.example.json to config/banks.json and fill in your credentials." >&2
  exit 1
fi

# ── Helpers ────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
header()  { echo -e "\n${BOLD}=== $* ===${RESET}"; }

# Wait for user to press Enter (used between TAN-triggering steps).
pause() {
  local msg="${1:-Press Enter to continue...}"
  echo -e "${YELLOW}>>> ${msg}${RESET}"
  read -r
}

# Returns 0 if the given login is already listed in aqhbci-tool4.
account_already_registered() {
  local login="$1"
  aqhbci-tool4 listusers 2>/dev/null | grep -q "${login}"
}

# ── Per-account setup ──────────────────────────────────────────────────────────

setup_account() {
  local name="$1"
  local blz="$2"
  local url="$3"
  local login="$4"
  local hbci_version="$5"
  local tan_mode="$6"   # may be empty

  header "Setting up: ${name} (BLZ ${blz})"

  # ── Step 0: Skip if already registered ──────────────────────────────────────
  if account_already_registered "$login"; then
    warn "Login '${login}' already found in aqhbci-tool4 — skipping."
    return 0
  fi

  # ── Step 1: adduser ──────────────────────────────────────────────────────────
  info "Step 1/8 — Registering bank user..."
  aqhbci-tool4 adduser \
    -t pintan \
    --context=1 \
    -b "$blz" \
    -u "$login" \
    -s "$url" \
    -N "$name" \
    --hbciversion="$hbci_version"
  success "User registered."

  # Resolve user index: count registered users after adduser; default to 1.
  local user_index=1
  local _user_count
  if _user_count=$(aqhbci-tool4 listusers 2>/dev/null | grep -c "^User") &&
     (( _user_count > 0 )); then
    user_index="$_user_count"
  fi

  # ── Step 2: getsysid (PIN only, no TAN) ──────────────────────────────────────
  info "Step 2/8 — Initialising connection (getsysid)..."
  pause "Press Enter to start — you will be prompted for your PIN..."
  aqhbci-tool4 getsysid -u "$user_index"
  success "System ID retrieved."

  # ── Step 3: listitanmodes ────────────────────────────────────────────────────
  info "Step 3/8 — Listing available TAN modes..."
  echo ""
  aqhbci-tool4 listitanmodes -u "$user_index"
  echo ""

  # ── Step 4: select TAN mode ──────────────────────────────────────────────────
  info "Step 4/8 — Select TAN mode."
  local selected_tan_mode
  if [[ -n "$tan_mode" ]]; then
    read -rp "TAN mode [default: ${tan_mode}]: " selected_tan_mode
    selected_tan_mode="${selected_tan_mode:-${tan_mode}}"
  else
    read -rp "Enter TAN mode number from the list above: " selected_tan_mode
    while [[ -z "$selected_tan_mode" ]]; do
      read -rp "TAN mode is required. Enter TAN mode number: " selected_tan_mode
    done
  fi

  # ── Step 5: setitanmode ──────────────────────────────────────────────────────
  info "Step 5/8 — Setting TAN mode to ${selected_tan_mode}..."
  aqhbci-tool4 setitanmode -u "$user_index" -m "$selected_tan_mode"
  success "TAN mode set to ${selected_tan_mode}."

  # ── Step 6: getaccounts (TAN required) ──────────────────────────────────────
  info "Step 6/8 — Fetching account list from bank (getaccounts)..."
  pause "Press Enter to start — your banking app will ask you to confirm another TAN..."
  aqhbci-tool4 getaccounts -u "$user_index"
  success "Account list retrieved."

  # ── Step 7: listaccounts ─────────────────────────────────────────────────────
  info "Step 7/8 — Verifying registered accounts..."
  echo ""
  aqhbci-tool4 listaccounts -v
  echo ""

  # ── Step 8: getaccsepa ───────────────────────────────────────────────────────
  info "Step 8/8 — Fetching SEPA account data..."
  aqhbci-tool4 getaccsepa -u "$user_index" || warn "getaccsepa failed — this is non-fatal."
  success "SEPA data fetched."

  success "Setup complete for: ${name}"
}

# ── Main ───────────────────────────────────────────────────────────────────────

main() {
  echo -e "${BOLD}txporter — AqBanking Interactive Setup${RESET}"
  echo "Config: ${CONFIG_FILE}"
  echo ""

  local accounts
  accounts=$(jq -c '.accounts[] | select(.type == "fints")' "$CONFIG_FILE")

  if [[ -z "$accounts" ]]; then
    warn "No FinTS accounts found in ${CONFIG_FILE}. Nothing to set up."
    exit 0
  fi

  local count
  count=$(echo "$accounts" | wc -l)
  info "Found ${count} FinTS account(s) to configure."

  local errors=0

  # Use fd 3 for account iteration so fd 0 (stdin) stays connected to the
  # user's terminal for pause prompts and TAN-mode input inside setup_account.
  local accounts_file
  accounts_file=$(mktemp)
  echo "$accounts" > "$accounts_file"

  while IFS= read -r -u 3 account; do
    local name blz url login hbci_version tan_mode
    name=$(echo "$account"         | jq -r '.name')
    blz=$(echo "$account"          | jq -r '.blz')
    url=$(echo "$account"          | jq -r '.url')
    login=$(echo "$account"        | jq -r '.login')
    hbci_version=$(echo "$account" | jq -r '.hbci_version // 300')
    tan_mode=$(echo "$account"     | jq -r '.tan_mode // empty')

    if setup_account "$name" "$blz" "$url" "$login" "$hbci_version" "$tan_mode"; then
      :
    else
      error "Setup failed for: ${name}"
      errors=$((errors + 1))
    fi
  done 3< "$accounts_file"
  rm -f "$accounts_file"

  echo ""
  if [[ $errors -eq 0 ]]; then
    success "All accounts configured successfully."
  else
    error "${errors} account(s) failed. Review the output above."
    exit 1
  fi
}

main "$@"
