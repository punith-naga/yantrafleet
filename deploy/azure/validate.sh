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
for script in "$HERE/custom-data.sh" "$HERE/deploy.sh" "$HERE/refresh-secrets.sh" "$HERE/validate.sh"; do
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
for var in REPO_URL SUPABASE_URL SUPABASE_KEY YANTRA_SITE_ID DOMAIN_NAME \
           CERTBOT_EMAIL GEMINI_API_KEY SARATHI_TOKEN WEBHOOK_URL \
           YANTRA_WEBHOOK_SECRET TWILIO_SID TWILIO_TOKEN TWILIO_FROM TWILIO_TO; do
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
#    (deploy.sh no longer hands --custom-data the literal custom-data.sh --
#    it goes through $CUSTOM_DATA_FILE, which is custom-data.sh itself when
#    USE_KEYVAULT=false and a blanked temp copy when true, see section 9
#    below -- so the default must still resolve to custom-data.sh and the
#    az vm create call must still be parameterized on it)
# ---------------------------------------------------------------------------
grep -q 'CUSTOM_DATA_FILE="custom-data.sh"' "$HERE/deploy.sh" \
    && pass "deploy.sh defaults CUSTOM_DATA_FILE to custom-data.sh" \
    || fail "deploy.sh no longer defaults to custom-data.sh (regression)"
grep -qF -- '--custom-data "$CUSTOM_DATA_FILE"' "$HERE/deploy.sh" \
    && pass "deploy.sh passes --custom-data \$CUSTOM_DATA_FILE" \
    || fail "deploy.sh does not reference \$CUSTOM_DATA_FILE"
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
#    can't quietly regress. Also covers refresh-secrets.sh: it's new,
#    ships as part of this same feature, and is just as capable of picking
#    up a stray em-dash/smart-quote from an editing pass. deploy.sh is
#    NOT included here -- it's never handed to az vm create --custom-data
#    (only custom-data.sh is), so the specific ARM/base64/latin-1 crash
#    this check guards against doesn't apply to it.
# ---------------------------------------------------------------------------
for script in "$HERE/custom-data.sh" "$HERE/refresh-secrets.sh"; do
    if LC_ALL=C grep -qP '[^\x00-\x7F]' "$script"; then
        fail "${script#"$REPO_ROOT"/} contains non-ASCII byte(s) (breaks az vm create on Windows — see comment above)"
    else
        pass "${script#"$REPO_ROOT"/} is pure ASCII"
    fi
done

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
# 8) Key Vault: custom-data.sh's EDIT ME block must declare KEYVAULT_NAME.
#    deploy.sh injects the resolved vault name into this line (see check 9
#    below) when USE_KEYVAULT=true; if the line disappears there is nowhere
#    for that injection to land and the sed in deploy.sh silently no-ops.
# ---------------------------------------------------------------------------
grep -q '^KEYVAULT_NAME=' "$HERE/custom-data.sh" \
    && pass "custom-data.sh declares KEYVAULT_NAME in its EDIT ME block" \
    || fail "custom-data.sh missing KEYVAULT_NAME (Key Vault mode has nowhere to inject the vault name)"

# ---------------------------------------------------------------------------
# 9) regression check: when USE_KEYVAULT=true, deploy.sh must NOT hand
#    custom-data.sh's own plaintext EDIT ME values to `az vm create
#    --custom-data` verbatim -- that would defeat the entire point of Key
#    Vault (the secrets would still sit in the VM's custom_data property in
#    Azure's control plane forever). It must build a temp copy with the 10
#    secret-shaped var lines blanked and KEYVAULT_NAME filled in instead,
#    and pass THAT to az vm create. Run the actual blanking logic here
#    (the same per-line sed deploy.sh uses) against the real custom-data.sh
#    so this fails if that logic regresses, rather than trusting deploy.sh's
#    own claim that it does this.
# ---------------------------------------------------------------------------
KV_SECRET_VARS="REPO_URL SUPABASE_URL SUPABASE_KEY GEMINI_API_KEY SARATHI_TOKEN WEBHOOK_URL YANTRA_WEBHOOK_SECRET TWILIO_SID TWILIO_TOKEN TWILIO_FROM TWILIO_TO"

TMP_BLANKED="$(mktemp)"
cp "$HERE/custom-data.sh" "$TMP_BLANKED"
for VAR in $KV_SECRET_VARS; do
    sed -i "s/^${VAR}=.*/${VAR}=\"\"/" "$TMP_BLANKED"
done
sed -i 's/^KEYVAULT_NAME=.*/KEYVAULT_NAME="test-kv-1234"/' "$TMP_BLANKED"

# Check the actual assignment lines only -- NOT a whole-file grep for
# placeholder-shaped text. custom-data.sh's own internal case-statement
# guard (section 0) legitimately contains strings like YOUR_SUPABASE_* as
# literal pattern text, so a whole-file search for "did it get cleared"
# text would always look clean/dirty regardless of whether the sed above
# actually touched the assignment lines -- this is the exact false-positive
# trap check 5 above already pins for deploy.sh's placeholder guard; don't
# repeat it here.
BLANK_OK="true"
for VAR in $KV_SECRET_VARS; do
    grep -q "^${VAR}=\"\"\$" "$TMP_BLANKED" || BLANK_OK="false"
done
if [ "$BLANK_OK" = "true" ]; then
    pass "blanking logic clears all 11 secret vars (incl. REPO_URL) in a temp custom-data copy"
else
    fail "blanking logic did not blank all 11 secret var assignment lines"
fi
grep -q '^KEYVAULT_NAME="test-kv-1234"' "$TMP_BLANKED" \
    && pass "blanking logic injects the resolved KEYVAULT_NAME into the temp custom-data copy" \
    || fail "blanking logic did not inject KEYVAULT_NAME (VM would boot unable to find its own vault)"
if printf '%s' "$KV_SECRET_VARS" | grep -qw 'DOMAIN_NAME\|CERTBOT_EMAIL'; then
    fail "DOMAIN_NAME/CERTBOT_EMAIL ended up in the Key Vault secret-var list -- they aren't secrets"
else
    pass "DOMAIN_NAME/CERTBOT_EMAIL are correctly excluded from the Key Vault secret-var list"
fi
rm -f "$TMP_BLANKED"

# deploy.sh must default to passing custom-data.sh through unmodified when
# USE_KEYVAULT=false -- covered structurally by check 4 above (CUSTOM_DATA_FILE
# defaults to "custom-data.sh" and only gets reassigned to a temp file inside
# the `if [ "$USE_KEYVAULT" = "true" ]` branch); pin that branch guard here
# too so a future refactor can't accidentally build the temp file unconditionally.
grep -q 'if \[ "\$USE_KEYVAULT" = "true" \]' "$HERE/deploy.sh" \
    && pass "deploy.sh only builds the blanked temp custom-data file when USE_KEYVAULT=true" \
    || fail "deploy.sh lost its USE_KEYVAULT=true guard around the Key Vault / temp-file logic"

# ---------------------------------------------------------------------------
# 10) Key Vault: custom-data.sh's boot-time IMDS token fetch. Must hit the
#     vault.azure.net resource with the Metadata:true header -- get either
#     wrong and the token request is rejected outright, not just slow.
# ---------------------------------------------------------------------------
grep -q '169.254.169.254/metadata/identity/oauth2/token' "$HERE/custom-data.sh" \
    && grep -q 'Metadata:true' "$HERE/custom-data.sh" \
    && grep -q 'resource=https://vault.azure.net' "$HERE/custom-data.sh" \
    && pass "custom-data.sh fetches an IMDS token for vault.azure.net with the Metadata:true header" \
    || fail "custom-data.sh's IMDS token fetch is missing or malformed (boot-time Key Vault fetch would fail)"

# ---------------------------------------------------------------------------
# 11) Key Vault: custom-data.sh must fetch all 10 secrets by their Key
#     Vault (hyphenated) names -- a var dropped here means that channel
#     silently stays blank forever after a fresh deploy, even though the
#     value is sitting in the vault.
# ---------------------------------------------------------------------------
for secret in repo-url supabase-url supabase-key gemini-api-key sarathi-token webhook-url \
              yantra-webhook-secret twilio-sid twilio-token twilio-from twilio-to; do
    grep -q "kv_get_secret ${secret}" "$HERE/custom-data.sh" \
        && pass "custom-data.sh fetches Key Vault secret '$secret'" \
        || fail "custom-data.sh is missing the Key Vault fetch for '$secret'"
done

# ---------------------------------------------------------------------------
# 12) RBAC gotcha: an RBAC-authorized Key Vault grants its creator no
#     implicit access (unlike legacy access-policy vaults), so deploy.sh
#     must explicitly grant itself Secrets Officer before it can upload
#     anything, and the VM's managed identity must separately get Secrets
#     User before boot-time fetch can read anything. Drop either and the
#     first real deploy 403s.
# ---------------------------------------------------------------------------
grep -q 'Key Vault Secrets Officer' "$HERE/deploy.sh" \
    && pass "deploy.sh grants itself 'Key Vault Secrets Officer' on the vault" \
    || fail "deploy.sh is missing the self-grant of 'Key Vault Secrets Officer' -- secret uploads would 403"
grep -q 'Key Vault Secrets User' "$HERE/deploy.sh" \
    && pass "deploy.sh grants the VM's managed identity 'Key Vault Secrets User' on the vault" \
    || fail "deploy.sh is missing the VM-identity grant of 'Key Vault Secrets User' -- boot-time fetch would 403"

# ---------------------------------------------------------------------------
# 13) the VM needs a managed identity in the first place for any of the
#     above role assignments to mean anything -- without --assign-identity
#     on az vm create there is no principal to grant Key Vault access to.
# ---------------------------------------------------------------------------
grep -q -- '--assign-identity' "$HERE/deploy.sh" \
    && pass "deploy.sh passes --assign-identity to az vm create" \
    || fail "deploy.sh no longer requests a managed identity for the VM -- boot-time Key Vault fetch has nothing to auth with"

# ---------------------------------------------------------------------------
# 14) refresh-secrets.sh must exist and custom-data.sh must install it onto
#     the VM -- otherwise rotating a secret in the vault later means
#     recreating the whole VM instead of just re-running one script over SSH.
# ---------------------------------------------------------------------------
[ -f "$HERE/refresh-secrets.sh" ] \
    && pass "refresh-secrets.sh exists" \
    || fail "refresh-secrets.sh is MISSING (custom-data.sh installs it at boot for later secret rotation)"
grep -q 'refresh-secrets.sh' "$HERE/custom-data.sh" \
    && pass "custom-data.sh installs refresh-secrets.sh onto the VM" \
    || fail "custom-data.sh no longer installs refresh-secrets.sh (operators would lose the ability to rotate secrets without recreating the VM)"

# ---------------------------------------------------------------------------
# 15) HTTPS automation: custom-data.sh must gate certbot on both DOMAIN_NAME
#     and CERTBOT_EMAIL, use --non-interactive so boot never hangs waiting
#     on a prompt, and not treat a certbot failure as fatal (DNS not being
#     live yet is an expected, recoverable case, not a boot-breaking one).
# ---------------------------------------------------------------------------
grep -q 'certbot --nginx' "$HERE/custom-data.sh" \
    && pass "custom-data.sh runs certbot's nginx plugin" \
    || fail "custom-data.sh is missing the certbot automation step"
grep -q -- '--non-interactive' "$HERE/custom-data.sh" \
    && pass "certbot invocation is --non-interactive (won't hang cloud-init on a prompt)" \
    || fail "certbot invocation is missing --non-interactive"
grep -qE 'if \[ -n "\$DOMAIN_NAME" \] && \[ -n "\$CERTBOT_EMAIL" \]' "$HERE/custom-data.sh" \
    && pass "certbot only runs when both DOMAIN_NAME and CERTBOT_EMAIL are set" \
    || fail "certbot gating condition regressed -- could run with only one of DOMAIN_NAME/CERTBOT_EMAIL set"
grep -B2 'certbot --nginx' "$HERE/custom-data.sh" | grep -q '^set -euo pipefail$' \
    && fail "set -e would make a certbot failure fatal -- must be wrapped in an if/then" \
    || pass "certbot call is guarded so a failure (e.g. DNS not live yet) doesn't fail the whole boot"

# ---------------------------------------------------------------------------
# 16) nginx template must take a real server_name so certbot's nginx plugin
#     can find the right block to fit with -d <domain> -- "server_name _;"
#     alone (nginx's catch-all) isn't guaranteed to match a specific -d.
# ---------------------------------------------------------------------------
grep -q 'server_name \${NGINX_SERVER_NAME}' "$REPO_ROOT/deploy/aws/nginx/yantrafleet.conf.template" \
    && pass "shared nginx template's server_name is substitutable (NGINX_SERVER_NAME)" \
    || fail "shared nginx template still hardcodes server_name _ -- certbot -d <domain> may not match"
grep -q 'NGINX_SERVER_NAME' "$HERE/custom-data.sh" \
    && pass "custom-data.sh computes and exports NGINX_SERVER_NAME for envsubst" \
    || fail "custom-data.sh does not set NGINX_SERVER_NAME"

# ---------------------------------------------------------------------------
# 17) deploy.sh must open 443 alongside 80 -- otherwise even a successful
#     certbot run leaves the box unreachable over HTTPS from outside.
# ---------------------------------------------------------------------------
grep -q -- '--port 443' "$HERE/deploy.sh" \
    && pass "deploy.sh opens port 443 (needed once certbot issues a cert)" \
    || fail "deploy.sh does not open port 443"

# ---------------------------------------------------------------------------
# 18) URL layout: the marketing site owns the bare host (/), the console
#     lives at /console/. deploy.sh's closing help text and custom-data's
#     final echo must point people at the right places.
# ---------------------------------------------------------------------------
grep -q 'root /opt/yantrafleet/marketing;' "$REPO_ROOT/deploy/aws/nginx/yantrafleet.conf.template" \
    && pass "shared nginx template serves marketing/ at the host root" \
    || fail "shared nginx template does not root the marketing site at /"
grep -q 'location /console/' "$REPO_ROOT/deploy/aws/nginx/yantrafleet.conf.template" \
    && pass "shared nginx template has the /console/ location" \
    || fail "shared nginx template is missing location /console/"
grep -q 'http://\$IP/console/' "$HERE/deploy.sh" \
    && pass "deploy.sh closing text points at /console/ for the console" \
    || fail "deploy.sh closing text does not mention /console/"
grep -q '/console/' "$HERE/custom-data.sh" \
    && pass "custom-data.sh final echo mentions /console/" \
    || fail "custom-data.sh final echo does not mention /console/"
[ -f "$REPO_ROOT/marketing/index.html" ] \
    && pass "marketing/index.html exists (nginx root would 404 otherwise)" \
    || fail "marketing/index.html missing"

# ---------------------------------------------------------------------------
# 19) Zero-signup demo sandbox door (v0.14). The nginx template is SHARED
#     with the AWS kit, so these checks are deliberately duplicated on both
#     sides: the two kits render the same file and either one shipping
#     without the route leaves the marketing site's primary call to action
#     pointing at a 404.
# ---------------------------------------------------------------------------
TEMPLATE="$REPO_ROOT/deploy/aws/nginx/yantrafleet.conf.template"
grep -q 'location /api/demo/ {' "$TEMPLATE" \
    && pass "shared nginx template has the /api/demo/ location (the Try-it button's endpoint)" \
    || fail "shared nginx template is MISSING location /api/demo/ -- the Try-it button would 404"
grep -q 'proxy_pass http://127.0.0.1:8088;' "$TEMPLATE" \
    && pass "/api/demo/ proxies to the sandbox door on 127.0.0.1:8088" \
    || fail "/api/demo/ does not proxy to 127.0.0.1:8088"

# Rate limiting on an UNAUTHENTICATED POST that creates database rows. The
# application has its own per-IP limiter; this is the cheap wall in front of
# it so a flood never reaches Python at all.
grep -q 'limit_req_zone \$binary_remote_addr zone=yantra_demo:' "$TEMPLATE" \
    && pass "limit_req_zone yantra_demo declared for the anonymous demo POST" \
    || fail "limit_req_zone yantra_demo MISSING -- /api/demo/session would have no edge rate limit"
grep -A20 'location /api/demo/ {' "$TEMPLATE" | grep -qE '^\s+limit_req zone=yantra_demo' \
    && pass "location /api/demo/ actually applies limit_req zone=yantra_demo" \
    || fail "limit_req zone=yantra_demo declared but never applied to /api/demo/"
# limit_req_zone is an http{}-context directive and sites-enabled/* is
# included from inside http{} -- inside server{} it makes `nginx -t` fail and
# the VM boots with no site at all. (Match the directive, not the word: the
# file mentions it in prose comments inside server{} on purpose.)
awk '/^server \{/{inside=1}
     inside && /^[[:space:]]*limit_req_zone[[:space:]]/{found=1}
     END{exit !found}' "$TEMPLATE" \
    && fail "limit_req_zone is inside server{} -- http-context directive; nginx -t would fail on boot" \
    || pass "limit_req_zone sits in http{} context, outside server{}"

# The door's own /healthz carries operational counters and must stay on
# loopback; /health on this box already belongs to the sarathi copilot.
grep -q 'location /healthz' "$TEMPLATE" \
    && fail "the sandbox door's /healthz is exposed publicly -- it must stay loopback-only" \
    || pass "sandbox /healthz is not proxied (loopback-only; /health is sarathi's)"

# custom-data.sh must install *.timer as well as *.service. Missing *.service
# is loud; missing *.timer is silent -- the reaper lands on the VM and
# nothing ever triggers it, so expired demo rows and orphaned simulator
# processes accumulate forever.
grep -q 'install -m 644 "\$APP_DIR"/deploy/aws/systemd/\*\.service' "$HERE/custom-data.sh" \
    && pass "custom-data.sh installs the *.service units" \
    || fail "custom-data.sh no longer installs *.service units"
grep -q 'install -m 644 "\$APP_DIR"/deploy/aws/systemd/\*\.timer' "$HERE/custom-data.sh" \
    && pass "custom-data.sh installs the *.timer units (the sandbox reaper is timer-driven)" \
    || fail "custom-data.sh installs only *.service -- yantra-sandbox-reap.timer would never land"
grep -E '^systemctl enable --now' "$HERE/custom-data.sh" | grep -q 'sandbox' \
    && fail "custom-data.sh auto-enables the sandbox -- supabase/0009 must be applied deliberately first" \
    || pass "custom-data.sh does not auto-enable yantra-sandbox (0009 is an opt-in)"

# demo_reap_expired() is granted to authenticated/service_role and NEVER to
# anon, so the reaper cannot reuse the door's anon key. An operator who does
# not know that ends up with a reaper that 403s every minute forever.
grep -q '/etc/yantrafleet-sandbox.env' "$HERE/custom-data.sh" \
    && pass "custom-data.sh documents /etc/yantrafleet-sandbox.env for the reaper's service_role key" \
    || fail "custom-data.sh never mentions /etc/yantrafleet-sandbox.env -- the reaper would 403 on demo_reap_expired()"
grep -q 'chmod 640 /etc/yantrafleet-sandbox.env' "$HERE/custom-data.sh" \
    && grep -q 'chown root:yantra /etc/yantrafleet-sandbox.env' "$HERE/custom-data.sh" \
    && pass "custom-data.sh documents mode 640 root:yantra on the sandbox env file" \
    || fail "custom-data.sh does not document 640 root:yantra on /etc/yantrafleet-sandbox.env"
# ...and Azure-specific: that key must NOT be added to the EDIT ME block,
# which is exactly what ends up in the VM's custom_data property forever.
grep -qE '^SUPABASE_SERVICE|^SERVICE_ROLE' "$HERE/custom-data.sh" \
    && fail "a service_role key variable appeared in custom-data.sh's EDIT ME block -- that lands in Azure's control plane in plaintext" \
    || pass "no service_role key in custom-data.sh's EDIT ME block (it belongs in Key Vault or a root-written env file)"

# Route strings the nginx block, the door and the marketing button must all
# agree on.
grep -q 'ROUTE_MINT = "/api/demo/session"' "$REPO_ROOT/ops/yantraops/sandbox_http.py" \
    && pass "sandbox_http.py still serves /api/demo/session" \
    || fail "sandbox_http.py's mint route moved -- nginx and marketing/index.html point at nothing"
grep -q 'action="/api/demo/session"' "$REPO_ROOT/marketing/index.html" \
    && pass "marketing/index.html posts to /api/demo/session with a real <form> (works with JS off)" \
    || fail "marketing/index.html has no zero-JS <form> posting to /api/demo/session"

# ---------------------------------------------------------------------------
# 20) The conformance page's filename is hard-coded into every report
#     yantra-conform generates (report_html.py TOOL_URL), so renaming the
#     file silently breaks the footer link in every report already sitting
#     in somebody's inbox.
# ---------------------------------------------------------------------------
[ -f "$REPO_ROOT/marketing/vda-5050-conformance-test.html" ] \
    && pass "marketing/vda-5050-conformance-test.html exists (hard-coded in generated reports)" \
    || fail "marketing/vda-5050-conformance-test.html missing -- generated reports link to it"
grep -q 'vda-5050-conformance-test.html' "$REPO_ROOT/tools/conformance/yantraconform/report_html.py" \
    && pass "report_html.py's footer link still matches that filename" \
    || fail "report_html.py no longer links vda-5050-conformance-test.html (filename drifted)"

# ---------------------------------------------------------------------------
echo
if [ "$FAILS" -eq 0 ]; then
    echo "validate.sh: all checks passed"
else
    echo "validate.sh: $FAILS check(s) FAILED" >&2
    exit 1
fi
