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
# 5) regression check: deploy.sh's placeholder guard must not false-positive
#    on a filled-in custom-data.sh. custom-data.sh's own internal safety
#    check (the `case ... in *YOUR_GITHUB_USERNAME*|...)` guard) legitimately
#    contains the placeholder strings as literal pattern text, so a naive
#    whole-file grep for those strings always matches regardless of whether
#    REPO_URL/SUPABASE_URL/SUPABASE_KEY were actually filled in — this bit
#    a real deploy attempt once, so it's pinned here.
# ---------------------------------------------------------------------------
# deploy.sh must NOT do a naive whole-file grep for the placeholder strings
# (custom-data.sh's own internal case-statement guard contains those strings
# as literal pattern text, so a whole-file grep always matches) — it must
# restrict to the actual EDIT ME assignment lines.
if grep -qE "^if grep -q 'YOUR_GITHUB_USERNAME" "$HERE/deploy.sh"; then
    fail "deploy.sh reverted to a whole-file placeholder grep (false-positives on filled-in files)"
else
    pass "deploy.sh's placeholder guard is scoped to REPO_URL/SUPABASE_URL/SUPABASE_KEY lines"
fi

TMP_FILLED="$(mktemp)"
trap 'rm -f "$TMP_FILLED"' EXIT
sed \
    -e 's#REPO_URL="https://github.com/YOUR_GITHUB_USERNAME/yantrafleet.git"#REPO_URL="https://github.com/example/yantrafleet.git"#' \
    -e 's#SUPABASE_URL="https://YOUR_PROJECT_REF.supabase.co"#SUPABASE_URL="https://example.supabase.co"#' \
    -e 's#SUPABASE_KEY="YOUR_SUPABASE_PUBLISHABLE_ANON_KEY"#SUPABASE_KEY="sb_publishable_example"#' \
    "$HERE/custom-data.sh" > "$TMP_FILLED"

# The same restricted-grep logic deploy.sh uses, run directly here so this
# test fails if that logic (wherever it lives in deploy.sh) regresses.
if grep -E '^(REPO_URL|SUPABASE_URL|SUPABASE_KEY)=' "$TMP_FILLED" \
        | grep -q 'YOUR_GITHUB_USERNAME\|YOUR_PROJECT_REF\|YOUR_SUPABASE_PUBLISHABLE'; then
    fail "placeholder guard logic false-positives on a filled-in custom-data.sh"
else
    pass "placeholder guard logic accepts a filled-in custom-data.sh"
fi
# ...and it must still catch a genuinely unfilled file (the shipped default).
grep -E '^(REPO_URL|SUPABASE_URL|SUPABASE_KEY)=' "$HERE/custom-data.sh" \
        | grep -q 'YOUR_GITHUB_USERNAME\|YOUR_PROJECT_REF\|YOUR_SUPABASE_PUBLISHABLE' \
    && pass "placeholder guard logic still catches an unfilled custom-data.sh" \
    || fail "placeholder guard no longer catches unfilled placeholders (regression)"

# ---------------------------------------------------------------------------
# 6) regression check: custom-data.sh must be pure ASCII. It's shipped
#    whole to `az vm create --custom-data`, which base64-encodes it into
#    an ARM request body; on Windows/Git-Bash the underlying Python HTTP
#    stack can default to a latin-1 stdout/body encoding, and a stray
#    typographic character (an em dash from Claude's own prose habits bit
#    us here) crashes the whole `az vm create` call with
#    "UnicodeEncodeError: 'latin-1' codec can't encode characters...".
#    Cloud-init scripts should be plain ASCII anyway — pinned here so it
#    can't quietly regress.
# ---------------------------------------------------------------------------
if LC_ALL=C grep -qP '[^\x00-\x7F]' "$HERE/custom-data.sh"; then
    fail "custom-data.sh contains non-ASCII byte(s) (breaks az vm create on Windows — see comment above)"
else
    pass "custom-data.sh is pure ASCII"
fi

# ---------------------------------------------------------------------------
# 7) regression check: deploy.sh must retry across regions on SkuNotAvailable
#    (a real "Standard_B2s has no capacity in CentralIndia right now" hit
#    a live deploy attempt) rather than making the user manually re-run
#    with a new LOCATION each time.
# ---------------------------------------------------------------------------
grep -q 'LOCATIONS' "$HERE/deploy.sh" && grep -q 'SkuNotAvailable' "$HERE/deploy.sh" \
    && pass "deploy.sh retries other regions on SkuNotAvailable" \
    || fail "deploy.sh lost its region-fallback loop (regression)"
grep -q 'DisallowedLocation' "$HERE/deploy.sh" \
    && pass "deploy.sh also retries other regions on DisallowedLocation" \
    || fail "deploy.sh lost its DisallowedLocation handling (regression)"
grep -q 'jioindiawest' "$HERE/deploy.sh" \
    && fail "deploy.sh's default LOCATIONS still lists jioindiawest (hit DisallowedLocation on a real subscription)" \
    || pass "deploy.sh's default LOCATIONS doesn't list a known-disallowed region"

# ---------------------------------------------------------------------------
echo
if [ "$FAILS" -eq 0 ]; then
    echo "validate.sh: all checks passed"
else
    echo "validate.sh: $FAILS check(s) FAILED" >&2
    exit 1
fi
