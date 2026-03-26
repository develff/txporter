#!/usr/bin/env bats
# Tests for scripts/setup.sh
# Requires: bats-core (https://github.com/bats-core/bats-core)

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/scripts/setup.sh"

# ── Fixtures ───────────────────────────────────────────────────────────────────

CONFIG_WITH_FINTS='{"accounts":[{"id":"dkb","name":"DKB","type":"fints","blz":"12030000","url":"https://fints.dkb.de/fints","login":"testuser","hbci_version":300,"tan_mode":7940}],"targets":{}}'
CONFIG_NO_FINTS='{"accounts":[{"id":"paypal","name":"PayPal","type":"paypal","login":"user@example.com"}],"targets":{}}'
CONFIG_NO_TAN_MODE='{"accounts":[{"id":"dkb","name":"DKB","type":"fints","blz":"12030000","url":"https://fints.dkb.de/fints","login":"testuser","hbci_version":300}],"targets":{}}'

# Write a temporary config file and set CONFIG_FILE.
make_config() {
  local content="$1"
  local path="${BATS_TMPDIR}/banks_$$.json"
  echo "$content" > "$path"
  echo "$path"
}

# Stub PATH to intercept aqhbci-tool4 and jq calls.
setup_stubs() {
  STUB_DIR="${BATS_TMPDIR}/stubs_$$"
  mkdir -p "$STUB_DIR"

  # jq stub — delegate to real jq
  cat > "${STUB_DIR}/jq" <<'EOF'
#!/bin/bash
exec /usr/bin/jq "$@"
EOF

  # aqhbci-tool4 stub — records calls and simulates success
  cat > "${STUB_DIR}/aqhbci-tool4" <<'EOF'
#!/bin/bash
echo "aqhbci-tool4 $*" >> "${STUB_DIR}/calls.log"
case "$1" in
  listusers)    echo "" ;;           # no existing users
  adduser)      echo "User added" ;;
  getsysid)     echo "SysID ok" ;;
  listitanmodes) echo "Mode 7940: DKB App TAN" ;;
  setitanmode)  echo "TAN mode set" ;;
  getaccounts)  echo "Accounts fetched" ;;
  listaccounts) echo "Account: DE12300000001234567890" ;;
  getaccsepa)   echo "SEPA ok" ;;
  *)            echo "Unknown command: $1" >&2; exit 1 ;;
esac
EOF

  chmod +x "${STUB_DIR}/jq" "${STUB_DIR}/aqhbci-tool4"
  export PATH="${STUB_DIR}:${PATH}"
  export STUB_DIR
}

# ── Tests ──────────────────────────────────────────────────────────────────────

@test "exits with error when jq is missing" {
  STUB_DIR="${BATS_TMPDIR}/nojq_$$"
  mkdir -p "$STUB_DIR"
  # provide aqhbci-tool4 but NOT jq
  cat > "${STUB_DIR}/aqhbci-tool4" <<'EOF'
#!/bin/bash
EOF
  chmod +x "${STUB_DIR}/aqhbci-tool4"

  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run env PATH="${STUB_DIR}" bash "$SCRIPT" --config "$cfg"
  [ "$status" -ne 0 ]
  [[ "$output" == *"jq"* ]]
}

@test "exits with error when aqhbci-tool4 is missing" {
  STUB_DIR="${BATS_TMPDIR}/noaqb_$$"
  mkdir -p "$STUB_DIR"
  cat > "${STUB_DIR}/jq" <<'EOF'
#!/bin/bash
exec /usr/bin/jq "$@"
EOF
  chmod +x "${STUB_DIR}/jq"

  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run env PATH="${STUB_DIR}" bash "$SCRIPT" --config "$cfg"
  [ "$status" -ne 0 ]
  [[ "$output" == *"aqhbci-tool4"* ]]
}

@test "exits with error when config file does not exist" {
  setup_stubs
  run bash "$SCRIPT" --config "/nonexistent/banks.json"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Config file not found"* ]]
}

@test "--config flag overrides default path" {
  setup_stubs
  cfg=$(make_config "$CONFIG_NO_FINTS")
  # Should succeed (0 FinTS accounts → warning + exit 0)
  run bash "$SCRIPT" --config "$cfg" <<< ""
  [ "$status" -eq 0 ]
  [[ "$output" == *"No FinTS accounts"* ]]
}

@test "skips non-FinTS (PayPal) accounts" {
  setup_stubs
  cfg=$(make_config "$CONFIG_NO_FINTS")
  run bash "$SCRIPT" --config "$cfg"
  [ "$status" -eq 0 ]
  [[ "$output" == *"No FinTS accounts"* ]]
  # aqhbci-tool4 should never have been called for setup
  [[ ! -f "${STUB_DIR}/calls.log" ]] || ! grep -q "adduser" "${STUB_DIR}/calls.log"
}

@test "all 8 aqhbci-tool4 steps are called for a new FinTS account" {
  setup_stubs
  cfg=$(make_config "$CONFIG_WITH_FINTS")
  # Provide Enter presses for pause prompts and TAN mode prompt (default 7940)
  run bash "$SCRIPT" --config "$cfg" <<< $'\n\n\n'
  [ "$status" -eq 0 ]
  grep -q "adduser"      "${STUB_DIR}/calls.log"
  grep -q "getsysid"     "${STUB_DIR}/calls.log"
  grep -q "listitanmodes" "${STUB_DIR}/calls.log"
  grep -q "setitanmode"  "${STUB_DIR}/calls.log"
  grep -q "getaccounts"  "${STUB_DIR}/calls.log"
  grep -q "listaccounts" "${STUB_DIR}/calls.log"
  grep -q "getaccsepa"   "${STUB_DIR}/calls.log"
}

@test "skips account that is already registered" {
  setup_stubs
  # Override listusers to return the login so account_already_registered returns true
  cat > "${STUB_DIR}/aqhbci-tool4" <<'EOF'
#!/bin/bash
echo "aqhbci-tool4 $*" >> "${STUB_DIR}/calls.log"
case "$1" in
  listusers) echo "User testuser" ;;
  *)         ;;
esac
EOF
  chmod +x "${STUB_DIR}/aqhbci-tool4"

  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run bash "$SCRIPT" --config "$cfg"
  [ "$status" -eq 0 ]
  [[ "$output" == *"skipping"* ]]
  ! grep -q "adduser" "${STUB_DIR}/calls.log" 2>/dev/null
}

@test "uses tan_mode from config as default in prompt" {
  setup_stubs
  cfg=$(make_config "$CONFIG_WITH_FINTS")
  # Just press Enter to accept the default TAN mode (7940)
  run bash "$SCRIPT" --config "$cfg" <<< $'\n\n\n'
  [ "$status" -eq 0 ]
  grep -q "setitanmode.*7940" "${STUB_DIR}/calls.log"
}

@test "prompts for TAN mode when not set in config" {
  setup_stubs
  cfg=$(make_config "$CONFIG_NO_TAN_MODE")
  # Provide TAN mode interactively + Enter presses for pauses
  run bash "$SCRIPT" --config "$cfg" <<< $'\n9999\n\n'
  [ "$status" -eq 0 ]
  grep -q "setitanmode.*9999" "${STUB_DIR}/calls.log"
}
