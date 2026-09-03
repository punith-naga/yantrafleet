#!/usr/bin/env bash
# Local sanity checks for the deploy/azure kit. Run from anywhere:
#   bash deploy/azure/validate.sh
# Exits non-zero on the first failure; prints PASS lines as it goes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
FAILS=0

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; FAILS=$((FAILS + 1)); }

# ---------------------------------------------------------------------------
# 1) bash -n every shell script in the kit
# ---------------------------------------------------------------------------
for script in "$HERE/custom-data.sh" "$HERE/deploy.sh" "$HERE/validate.sh"; do
    if bash -n "$script"; then
        pass "bash -n ${script#"$REPO_ROOT"/}"
    else
        fail "bash -n ${script#"$REPO_ROOT"/}"
    fi
done

# ---------------------------------------------------------------------------
# 2) custom-data.sh EDIT ME block matches the AWS kit's shape (both must
#    stay in lockstep — same env vars, same env-file write) since this is
#    a deliberate fork of user-data.sh rather than a shared template.
# ---------------------------------------------------------------------------
for var in REPO_URL SUPABASE_URL SUPABASE_KEY YANTRA_SITE_ID GEMINI_API_KEY \
           SARATHI_TOKEN WEBHOOK_URL YANTRA_WEBHOOK_SECRET TWILIO_SID \
           TWILIO_TOKEN TWILIO_FROM TWILIO_TO; do
    grep -q "^${var}=" "$HERE/custom-data.sh" \
        && pass "custom-data.sh declares $var" \
        || fail "custom-data.sh missing $var"
done

# ---------------------------------------------------------------------------
# 3) the shared assets custom-data.sh depends on actually exist
# ---------------------------------------------------------------------------
for shared in "$REPO_ROOT/deploy/aws/systemd" "$REPO_ROOT/deploy/aws/nginx/yantrafleet.conf.template"; do
    [ -e "$shared" ] \
        && pass "shared asset present: ${shared#"$REPO_ROOT"/}" \
        || fail "shared asset MISSING: ${shared#"$REPO_ROOT"/} (custom-data.sh references it)"
done

# ---------------------------------------------------------------------------
# 4) deploy.sh references the right custom-data file and shared systemd unit
# ---------------------------------------------------------------------------
grep -q -- '--custom-data custom-data.sh' "$HERE/deploy.sh" \
    && pass "deploy.sh passes --custom-data custom-data.sh" \
    || fail "deploy.sh does not reference custom-data.sh"
grep -q -- '--host 127.0.0.1 --port 8001' "$REPO_ROOT/deploy/aws/systemd/yantra-sarathi.service" \
    && pass "yantra-sarathi.service (shared) binds 127.0.0.1:8001" \
    || fail "yantra-sarathi.service does not bind 127.0.0.1:8001"

# ---------------------------------------------------------------------------
echo
if [ "$FAILS" -eq 0 ]; then
    echo "validate.sh: all checks passed"
else
    echo "validate.sh: $FAILS check(s) FAILED" >&2
    exit 1
fi
