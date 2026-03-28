#!/usr/bin/env bats
# Tests for scripts/setup.sh
# Requires: bats-core (https://github.com/bats-core/bats-core)

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/scripts/setup.sh"

# ── Fixtures ───────────────────────────────────────────────────────────────────

CONFIG_WITH_FINTS='{"accounts":[{"id":"dkb","name":"DKB","type":"fints","blz":"12030000","url":"https://fints.dkb.de/fints","login":"testuser","hbci_version":300,"tan_mode":7940}],"targets":{}}'
CONFIG_NO_FINTS='{"accounts":[{"id":"paypal","name":"PayPal","type":"paypal","login":"user@example.com"}],"targets":{}}'
CONFIG_NO_TAN_MODE='{"accounts":[{"id":"dkb","name":"DKB","type":"fints","blz":"12030000","url":"https://fints.dkb.de/fints","login":"testuser","hbci_version":300}],"targets":{}}'

# Write a temporary config file.
make_config() {
  local content="$1"
  local path="${BATS_TEST_TMPDIR}/banks.json"
  echo "$content" > "$path"
  echo "$path"
}

# Write a BASH_ENV file that overrides 'command -v <tool>' to simulate missing.
# Usage: make_missing_env <tool> <output_path>
make_missing_env() {
  local tool="$1"
  local env_file="$2"
  cat > "$env_file" <<EOF
command() {
  if [[ "\$1" == "-v" && "\$2" == "${tool}" ]]; then
    return 1
  fi
  builtin command "\$@"
}
export -f command
EOF
}

# Stub PATH to intercept aqhbci-tool4 and jq calls.
# Uses BATS_TEST_TMPDIR (unique per test) so tests never share a calls log.
setup_stubs() {
  STUB_DIR="${BATS_TEST_TMPDIR}/stubs"
  mkdir -p "$STUB_DIR"

  # jq stub — delegate to real jq
  cat > "${STUB_DIR}/jq" <<'STUB'
#!/bin/bash
exec /usr/bin/jq "$@"
STUB

  # aqhbci-tool4 stub — records calls and simulates success.
  # STUB_DIR is expanded *now* (unquoted heredoc) so the absolute path is baked
  # into the stub file; no env variable needed at stub runtime.
  local log="${STUB_DIR}/calls.log"
  cat > "${STUB_DIR}/aqhbci-tool4" <<STUB
#!/bin/bash
echo "aqhbci-tool4 \$*" >> "${log}"
case "\$1" in
  listusers)
    if grep -q "^aqhbci-tool4 adduser" "${log}" 2>/dev/null; then
      echo "User 0: Bank: de/12030000 User Id: testuser Customer Id: testuser Unique Id: 1"
    fi
    ;;
  adduser)       echo "User added" ;;
  getsysid)      echo "SysID ok" ;;
  listitanmodes) echo "Mode 7940: DKB App TAN" ;;
  setitanmode)   echo "TAN mode set" ;;
  getaccounts)   echo "Accounts fetched" ;;
  listaccounts)  echo "Account: DE12300000001234567890" ;;
  getaccsepa)    echo "SEPA ok" ;;
  *)             echo "Unknown command: \$1" >&2; exit 1 ;;
esac
STUB

  chmod +x "${STUB_DIR}/jq" "${STUB_DIR}/aqhbci-tool4"
  export PATH="${STUB_DIR}:${PATH}"
  export STUB_DIR
}

# ── Tests ──────────────────────────────────────────────────────────────────────

@test "exits with error when jq is missing" {
  local env_file="${BATS_TEST_TMPDIR}/missing_jq.sh"
  make_missing_env "jq" "$env_file"
  # Also provide a stub aqhbci-tool4 so the script does not fail on that check
  local stub_dir="${BATS_TEST_TMPDIR}/stubs"
  mkdir -p "$stub_dir"
  printf '#!/bin/bash\necho stub\n' > "${stub_dir}/aqhbci-tool4"
  chmod +x "${stub_dir}/aqhbci-tool4"

  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run env BASH_ENV="$env_file" PATH="${stub_dir}:${PATH}" bash "$SCRIPT" --config "$cfg"
  [ "$status" -ne 0 ]
  [[ "$output" == *"jq"* ]]
}

@test "exits with error when aqhbci-tool4 is missing" {
  local env_file="${BATS_TEST_TMPDIR}/missing_aqb.sh"
  make_missing_env "aqhbci-tool4" "$env_file"

  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run env BASH_ENV="$env_file" bash "$SCRIPT" --config "$cfg"
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
  run bash "$SCRIPT" --config "$cfg"
  [ "$status" -eq 0 ]
  [[ "$output" == *"No FinTS accounts"* ]]
}

@test "skips non-FinTS (PayPal) accounts" {
  setup_stubs
  cfg=$(make_config "$CONFIG_NO_FINTS")
  run bash "$SCRIPT" --config "$cfg"
  [ "$status" -eq 0 ]
  [[ "$output" == *"No FinTS accounts"* ]]
  local log="${STUB_DIR}/calls.log"
  [[ ! -f "$log" ]] || ! grep -q "adduser" "$log"
}

@test "all 8 aqhbci-tool4 steps are called for a new FinTS account" {
  setup_stubs
  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run bash "$SCRIPT" --config "$cfg" <<< $'\n\n\n'
  [ "$status" -eq 0 ]
  local log="${STUB_DIR}/calls.log"
  grep -q "adduser"       "$log"
  grep -q "getsysid"      "$log"
  grep -q "listitanmodes" "$log"
  grep -q "setitanmode"   "$log"
  grep -q "getaccounts"   "$log"
  grep -q "listaccounts"  "$log"
  grep -q "getaccsepa"    "$log"
}

@test "skips account that is already registered" {
  setup_stubs
  local log="${STUB_DIR}/calls.log"

  # Override stub: listusers returns the login → account_already_registered = true
  cat > "${STUB_DIR}/aqhbci-tool4" <<STUB
#!/bin/bash
echo "aqhbci-tool4 \$*" >> "${log}"
case "\$1" in
  listusers) echo "User testuser" ;;
  *)         ;;
esac
STUB
  chmod +x "${STUB_DIR}/aqhbci-tool4"

  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run bash "$SCRIPT" --config "$cfg"
  [ "$status" -eq 0 ]
  [[ "$output" == *"skipping"* ]]
  ! grep -q "adduser" "$log" 2>/dev/null
}

@test "uses tan_mode from config as default in prompt" {
  setup_stubs
  cfg=$(make_config "$CONFIG_WITH_FINTS")
  run bash "$SCRIPT" --config "$cfg" <<< $'\n\n\n'
  [ "$status" -eq 0 ]
  grep -q "setitanmode.*7940" "${STUB_DIR}/calls.log"
}

@test "prompts for TAN mode when not set in config" {
  setup_stubs
  cfg=$(make_config "$CONFIG_NO_TAN_MODE")
  run bash "$SCRIPT" --config "$cfg" <<< $'\n9999\n\n'
  [ "$status" -eq 0 ]
  grep -q "setitanmode.*9999" "${STUB_DIR}/calls.log"
}
