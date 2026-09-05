#!/usr/bin/env bash
# Local sanity checks for the deploy/aws kit. Run from anywhere:
#   bash deploy/aws/validate.sh
# Exits non-zero on the first failure; prints PASS lines as it goes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
FAILS=0

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; FAILS=$((FAILS + 1)); }

# ---------------------------------------------------------------------------
# 1) bash -n every shell script in the kit (+ the installer user-data calls)
# ---------------------------------------------------------------------------
for script in "$HERE/user-data.sh" "$HERE/validate.sh" "$REPO_ROOT/install.sh"; do
    if bash -n "$script"; then
        pass "bash -n ${script#"$REPO_ROOT"/}"
    else
        fail "bash -n ${script#"$REPO_ROOT"/}"
    fi
done

# ---------------------------------------------------------------------------
# 2) nginx template: envsubst dry-run with dummy values
# ---------------------------------------------------------------------------
TEMPLATE="$HERE/nginx/yantrafleet.conf.template"
export SUPABASE_URL="https://dummy.supabase.co"
export SUPABASE_KEY="dummy-anon-key"
export YANTRA_SITE_ID="TEST-SITE"
export NGINX_SERVER_NAME="example.test"

if command -v envsubst >/dev/null 2>&1; then
    RENDERED="$(envsubst '${SUPABASE_URL} ${SUPABASE_KEY} ${YANTRA_SITE_ID} ${NGINX_SERVER_NAME}' < "$TEMPLATE")"
    pass "envsubst dry-run of nginx template"
else
    # envsubst (gettext-base) missing locally — same substitution in python.
    RENDERED="$(python3 - "$TEMPLATE" <<'PY'
import os, sys
text = open(sys.argv[1]).read()
for var in ("SUPABASE_URL", "SUPABASE_KEY", "YANTRA_SITE_ID", "NGINX_SERVER_NAME"):
    text = text.replace("${%s}" % var, os.environ[var])
print(text)
PY
)"
    pass "nginx template dry-run (python fallback; envsubst not installed here)"
fi

# marketing site owns the bare host root; console moved to /console/
echo "$RENDERED" | grep -q 'root /opt/yantrafleet/marketing;' \
    && pass "server root is the marketing site (/ = landing page)" \
    || fail "server root is not /opt/yantrafleet/marketing"
echo "$RENDERED" | grep -q '302 /console/index.html?supa=https://dummy.supabase.co&key=dummy-anon-key&site=TEST-SITE' \
    && pass "console 302 redirect (/console/) carries supa/key/site" \
    || fail "console 302 redirect missing substituted supa/key/site"
echo "$RENDERED" | grep -q 'location /console/ {' \
    && pass "nginx location /console/ present" \
    || fail "nginx location /console/ missing"
echo "$RENDERED" | grep -q 'return 302 /index.html' \
    && fail "stale root-level console redirect still present (would shadow the marketing site)" \
    || pass "no stale root-level console redirect"
echo "$RENDERED" | grep -q '${SUPABASE\|${YANTRA\|${NGINX' \
    && fail "unsubstituted \${...} placeholders remain in rendered nginx conf" \
    || pass "no leftover \${...} placeholders"
echo "$RENDERED" | grep -q 'server_name example.test;' \
    && pass "server_name carries the substituted NGINX_SERVER_NAME (needed for certbot --nginx -d <domain> to find this block)" \
    || fail "server_name did not pick up NGINX_SERVER_NAME"
# nginx's own runtime variables must survive substitution untouched
for nvar in '$uri' '$host' '$remote_addr' '$scheme'; do
    echo "$RENDERED" | grep -qF "$nvar" \
        && pass "nginx variable $nvar preserved" \
        || fail "nginx variable $nvar was clobbered"
done
echo "$RENDERED" | grep -q 'proxy_pass http://127.0.0.1:8001;' \
    && pass "/ask + /health proxy to 127.0.0.1:8001" \
    || fail "copilot proxy_pass missing"

# academy + docs static locations (v0.9.x)
echo "$RENDERED" | grep -q 'location /academy/' \
    && pass "nginx location /academy/ present" \
    || fail "nginx location /academy/ missing"
echo "$RENDERED" | grep -q 'location /docs/' \
    && pass "nginx location /docs/ present" \
    || fail "nginx location /docs/ missing"
echo "$RENDERED" | grep -q '302 /academy/index.html?supa=https://dummy.supabase.co&key=dummy-anon-key&site=TEST-SITE' \
    && pass "academy 302 redirect carries supa/key/site" \
    || fail "academy 302 redirect missing substituted supa/key/site"
echo "$RENDERED" | grep -qE 'location /(academy|docs)/ \{' \
    && echo "$RENDERED" | grep -q 'root /opt/yantrafleet;' \
    && pass "academy/docs served from /opt/yantrafleet" \
    || fail "academy/docs root directive missing"

# -- zero-signup demo sandbox door (v0.14) ---------------------------------
# The marketing page's "Try it with a live fleet" button POSTs to
# /api/demo/session. Without this location the button is a 404 and the whole
# acquisition funnel is dead, so it is pinned here.
echo "$RENDERED" | grep -q 'location /api/demo/ {' \
    && pass "nginx location /api/demo/ present (the Try-it button's endpoint)" \
    || fail "nginx location /api/demo/ MISSING -- the marketing Try-it button would 404"
echo "$RENDERED" | grep -q 'proxy_pass http://127.0.0.1:8088;' \
    && pass "/api/demo/ proxies to the sandbox door on 127.0.0.1:8088" \
    || fail "/api/demo/ does not proxy to 127.0.0.1:8088"
# X-Forwarded-For is not cosmetic here: yantra-sandbox runs with
# --trusted-proxy-hops 1, so without this header every visitor is 127.0.0.1
# and the per-IP quota becomes one global quota for the whole internet.
echo "$RENDERED" | grep -A20 'location /api/demo/ {' \
    | grep -q 'proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;' \
    && pass "/api/demo/ forwards X-Forwarded-For (--trusted-proxy-hops 1 needs it for the per-IP quota)" \
    || fail "/api/demo/ does not forward X-Forwarded-For -- every visitor would share one quota"

# nginx-layer rate limiting. This is an UNAUTHENTICATED POST that creates
# database rows and forks a simulator, so the application's own limiter must
# not be the only thing in front of it.
echo "$RENDERED" | grep -q 'limit_req_zone \$binary_remote_addr zone=yantra_demo:' \
    && pass "limit_req_zone yantra_demo declared (rate limiting on the anonymous POST)" \
    || fail "limit_req_zone yantra_demo MISSING -- /api/demo/session would have no edge rate limit"
echo "$RENDERED" | grep -A20 'location /api/demo/ {' | grep -qE '^\s+limit_req zone=yantra_demo' \
    && pass "location /api/demo/ actually applies limit_req zone=yantra_demo" \
    || fail "limit_req zone=yantra_demo declared but never applied to /api/demo/"
# limit_req_zone is an http{}-context directive. sites-enabled/* is included
# from inside http{}, so it must sit OUTSIDE the server{} block -- inside it,
# `nginx -t` fails outright and the box boots with no site at all.
# (match the DIRECTIVE, not the word: this file mentions limit_req_zone in
#  prose comments inside the server block on purpose)
echo "$RENDERED" | awk '/^server \{/{inside=1}
                        inside && /^[[:space:]]*limit_req_zone[[:space:]]/{found=1}
                        END{exit !found}' \
    && fail "limit_req_zone is inside server{} -- that is an http-context directive; nginx -t would fail" \
    || pass "limit_req_zone sits in http{} context (outside server{}), where nginx requires it"
echo "$RENDERED" | grep -qF '$binary_remote_addr' \
    && pass "nginx variable \$binary_remote_addr preserved through envsubst" \
    || fail "nginx variable \$binary_remote_addr was clobbered"

# /healthz must NOT be public: it carries operational counters, and /health
# on this box already belongs to the sarathi copilot.
echo "$RENDERED" | grep -q 'location /healthz\|proxy_pass http://127.0.0.1:8088/healthz' \
    && fail "the sandbox door's /healthz is exposed publicly -- it must stay loopback-only" \
    || pass "sandbox /healthz is not proxied (stays loopback-only, /health is sarathi's)"

# Every anonymous-reachable location must say why it is safe to expose.
# This is a documentation lint, not a security control: it exists so that
# adding a publicly reachable route without writing down who can reach it
# and why that is acceptable fails loudly right here. Two halves, because
# either alone is easy to fool: one "# ANONYMOUS:" marker must sit within
# the 50 lines above each route (the comment blocks in that file are long),
# AND the total marker count must equal the number of route groups, so
# deleting one route's rationale cannot be papered over by its neighbour's.
ANON_ROUTES=('location / {' 'location /console/ {' 'location /academy/ {'
             'location /docs/ {' 'location ~ ^/docs/.*\.md$ {'
             'location /api/demo/ {' 'location /ask {' 'location /health {')
for anon in "${ANON_ROUTES[@]}"; do
    if echo "$RENDERED" | awk -v want="$anon" '
            /# ANONYMOUS/ { last = NR }
            index($0, want) && !seen { seen = 1; ok = (last > 0 && NR - last <= 50) }
            END { exit !ok }'; then
        pass "anonymous-route rationale documented above \"$anon\""
    else
        fail "no '# ANONYMOUS:' rationale comment above \"$anon\" (every public route needs one)"
    fi
done
ANON_MARKERS="$(echo "$RENDERED" | grep -c '# ANONYMOUS')"
[ "$ANON_MARKERS" -eq "${#ANON_ROUTES[@]}" ] \
    && pass "one '# ANONYMOUS:' rationale per public route group ($ANON_MARKERS of ${#ANON_ROUTES[@]})" \
    || fail "$ANON_MARKERS '# ANONYMOUS:' markers for ${#ANON_ROUTES[@]} public route groups -- one route's rationale is missing or a route was added without one"

# ---------------------------------------------------------------------------
# 3) python sanity (the instance needs a venv-capable python3)
# ---------------------------------------------------------------------------
python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
    && pass "python3 -c sanity (>= 3.10)" \
    || fail "python3 sanity"

# ---------------------------------------------------------------------------
# 4) systemd units: systemd-analyze verify when usable, else grep lint
# ---------------------------------------------------------------------------
UNITS=("$HERE"/systemd/*.service)

# `systemd-analyze verify` resolves User= and ExecStart= paths against the
# *local* machine, so it only gives meaningful results on a deployed box
# (yantra user + /opt/yantrafleet/.venv present). Elsewhere, lint by grep.
if command -v systemd-analyze >/dev/null 2>&1 \
        && id -u yantra >/dev/null 2>&1 \
        && [ -x /opt/yantrafleet/.venv/bin/python ]; then
    for unit in "${UNITS[@]}"; do
        if systemd-analyze verify "$unit"; then
            pass "systemd-analyze verify $(basename "$unit")"
        else
            fail "systemd-analyze verify $(basename "$unit")"
        fi
    done
else
    echo "note: systemd-analyze verify skipped (not on a deployed instance);" \
         "using grep-based unit lint"
    # The lint reads a LOGICALLY-JOINED copy of each unit: systemd lets a
    # directive be continued onto the next line with a trailing backslash,
    # and yantra-sandbox.service uses that for its long ExecStart. Linting
    # the raw file flags every continuation line as a stray key, which is
    # wrong -- join them first, exactly as systemd does.
    JOINED="$(mktemp)"
    trap 'rm -f "$JOINED"' EXIT
    for unit in "${UNITS[@]}"; do
        name="$(basename "$unit")"
        ok=1
        sed -e :a -e '/\\$/N; s/\\\n[[:space:]]*/ /; ta' "$unit" > "$JOINED"

        # Every unit needs these, whatever its type.
        for required in '^\[Unit\]' '^\[Service\]' '^Description=' \
                        '^ExecStart=/opt/yantrafleet/\.venv/bin/python' \
                        '^EnvironmentFile=/etc/yantrafleet\.env' \
                        '^User=yantra'; do
            grep -q "$required" "$JOINED" || { fail "$name: missing '$required'"; ok=0; }
        done

        # Restart=always / [Install] / WantedBy=multi-user.target belong to
        # long-running daemons. A Type=oneshot unit is a task, not a daemon:
        # it is *meant* to exit, restarting it would be a bug, and it is
        # started by a .timer rather than by multi-user.target -- so it has
        # no [Install] section at all. Require the daemon keys only of
        # daemons, and require a driving timer of a oneshot instead, so
        # "installed but nothing ever runs it" still fails this lint.
        if grep -q '^Type=oneshot' "$JOINED"; then
            timer="${unit%.service}.timer"
            if [ -f "$timer" ] && grep -q "^Unit=$name\$" "$timer"; then
                pass "oneshot $name is driven by $(basename "$timer")"
            else
                fail "$name is Type=oneshot but no .timer in this kit runs it"
                ok=0
            fi
        else
            for required in '^\[Install\]' '^Restart=always' \
                            '^WantedBy=multi-user\.target'; do
                grep -q "$required" "$JOINED" || { fail "$name: missing '$required'"; ok=0; }
            done
        fi

        # no stray keys outside a section; every non-blank/comment line is
        # either a [Section] or key=value
        grep -vE '^\s*(#|$|\[[A-Za-z]+\]|[A-Za-z][A-Za-z0-9]*=)' "$JOINED" \
            | grep -q . && { fail "$name: malformed line(s)"; ok=0; }
        [ "$ok" -eq 1 ] && pass "unit lint $name"
    done
    rm -f "$JOINED"
fi

# ---------------------------------------------------------------------------
# 4b) .timer units. These are linted separately because a timer has no
#     [Service] section and no ExecStart -- the loop above would reject
#     every one of them. They are ALSO the reason section 6 below checks
#     that both boot scripts install *.timer: a timer that never reaches
#     /etc/systemd/system is a reaper that never runs.
# ---------------------------------------------------------------------------
TIMERS=("$HERE"/systemd/*.timer)
if [ -e "${TIMERS[0]}" ]; then
    for timer in "${TIMERS[@]}"; do
        tname="$(basename "$timer")"
        tok=1
        for required in '^\[Unit\]' '^\[Timer\]' '^\[Install\]' \
                        '^Description=' '^Unit=' '^WantedBy=timers\.target'; do
            grep -q "$required" "$timer" || { fail "$tname: missing '$required'"; tok=0; }
        done
        # the .service it drives must exist in this same kit
        driven="$(sed -n 's/^Unit=//p' "$timer" | head -1)"
        [ -n "$driven" ] && [ -f "$HERE/systemd/$driven" ] \
            || { fail "$tname: Unit=$driven is not a unit in this kit"; tok=0; }
        grep -vE '^\s*(#|$|\[[A-Za-z]+\]|[A-Za-z][A-Za-z0-9]*=)' "$timer" \
            | grep -q . && { fail "$tname: malformed line(s)"; tok=0; }
        [ "$tok" -eq 1 ] && pass "timer lint $tname"
    done
else
    fail "no .timer units in deploy/aws/systemd (yantra-sandbox-reap.timer expected)"
fi

# sarathi must bind loopback:8001 (the nginx proxy target)
grep -q -- '--host 127.0.0.1 --port 8001' "$HERE/systemd/yantra-sarathi.service" \
    && pass "yantra-sarathi binds 127.0.0.1:8001" \
    || fail "yantra-sarathi does not bind 127.0.0.1:8001"

# ---------------------------------------------------------------------------
# HTTPS automation: user-data.sh must gate certbot on both DOMAIN_NAME and
# CERTBOT_EMAIL, use --non-interactive so boot never hangs waiting on a
# prompt, and not treat a certbot failure as fatal (DNS not being live yet
# is an expected, recoverable case, not a boot-breaking one).
# ---------------------------------------------------------------------------
grep -q 'certbot --nginx' "$HERE/user-data.sh" \
    && pass "user-data.sh runs certbot's nginx plugin" \
    || fail "user-data.sh is missing the certbot automation step"
grep -q -- '--non-interactive' "$HERE/user-data.sh" \
    && pass "certbot invocation is --non-interactive (won't hang cloud-init on a prompt)" \
    || fail "certbot invocation is missing --non-interactive"
grep -qE 'if \[ -n "\$DOMAIN_NAME" \] && \[ -n "\$CERTBOT_EMAIL" \]' "$HERE/user-data.sh" \
    && pass "certbot only runs when both DOMAIN_NAME and CERTBOT_EMAIL are set" \
    || fail "certbot gating condition regressed -- could run with only one of DOMAIN_NAME/CERTBOT_EMAIL set"
grep -q 'NGINX_SERVER_NAME' "$HERE/user-data.sh" \
    && pass "user-data.sh computes and exports NGINX_SERVER_NAME for envsubst" \
    || fail "user-data.sh does not set NGINX_SERVER_NAME"

# ---------------------------------------------------------------------------
# 6) systemd unit INSTALLATION at boot. Two separate globs, two separate
#    failure modes:
#      *.service missing -> nothing runs at all (loud, obvious)
#      *.timer   missing -> yantra-sandbox-reap.service lands on the box but
#                           nothing ever triggers it, so expired demo rows
#                           and orphaned simulator processes pile up quietly
#                           forever. That one is silent, which is worse.
# ---------------------------------------------------------------------------
grep -q 'install -m 644 "\$APP_DIR"/deploy/aws/systemd/\*\.service' "$HERE/user-data.sh" \
    && pass "user-data.sh installs the *.service units" \
    || fail "user-data.sh no longer installs *.service units"
grep -q 'install -m 644 "\$APP_DIR"/deploy/aws/systemd/\*\.timer' "$HERE/user-data.sh" \
    && pass "user-data.sh installs the *.timer units (the sandbox reaper is timer-driven)" \
    || fail "user-data.sh installs only *.service -- yantra-sandbox-reap.timer would never land"

# The sandbox must NOT be auto-enabled: it grants anonymous write access and
# only makes sense once supabase/0009 has been deliberately applied.
grep -E '^systemctl enable --now' "$HERE/user-data.sh" | grep -q 'sandbox' \
    && fail "user-data.sh auto-enables the sandbox -- supabase/0009 must be applied deliberately first" \
    || pass "user-data.sh does not auto-enable yantra-sandbox (0009 is an opt-in)"

# ...but it must tell the operator how to turn it on, including the second
# env file. demo_reap_expired() is granted to authenticated/service_role and
# never to anon, so the reaper cannot reuse the door's anon key -- an
# operator who does not know that ends up with a reaper that 403s every
# minute forever.
grep -q '/etc/yantrafleet-sandbox.env' "$HERE/user-data.sh" \
    && pass "user-data.sh documents /etc/yantrafleet-sandbox.env for the reaper's service_role key" \
    || fail "user-data.sh never mentions /etc/yantrafleet-sandbox.env -- the reaper would 403 on demo_reap_expired()"
grep -q 'chmod 640 /etc/yantrafleet-sandbox.env' "$HERE/user-data.sh" \
    && grep -q 'chown root:yantra /etc/yantrafleet-sandbox.env' "$HERE/user-data.sh" \
    && pass "user-data.sh documents mode 640 root:yantra on the sandbox env file" \
    || fail "user-data.sh does not document 640 root:yantra on /etc/yantrafleet-sandbox.env"
grep -q 'systemctl enable --now yantra-sandbox yantra-sandbox-reap.timer' "$HERE/user-data.sh" \
    && pass "user-data.sh documents enabling BOTH the door and the reap timer" \
    || fail "user-data.sh does not document enabling yantra-sandbox + yantra-sandbox-reap.timer"

# The door's bind address has to match what nginx proxies to.
grep -q -- '--host 127.0.0.1 --port 8088' "$HERE/systemd/yantra-sandbox.service" \
    && pass "yantra-sandbox binds 127.0.0.1:8088 (nginx's /api/demo/ proxy target)" \
    || fail "yantra-sandbox does not bind 127.0.0.1:8088"
grep -q -- '--trusted-proxy-hops 1' "$HERE/systemd/yantra-sandbox.service" \
    && pass "yantra-sandbox trusts exactly one proxy hop (nginx) for X-Forwarded-For" \
    || fail "yantra-sandbox is missing --trusted-proxy-hops 1 -- per-IP quota would be charged to nginx"
grep -q '^EnvironmentFile=-/etc/yantrafleet-sandbox.env' "$HERE/systemd/yantra-sandbox-reap.service" \
    && pass "yantra-sandbox-reap reads /etc/yantrafleet-sandbox.env (optional, second, wins)" \
    || fail "yantra-sandbox-reap does not read /etc/yantrafleet-sandbox.env"
grep -q '^EnvironmentFile=/etc/yantrafleet-sandbox.env' "$HERE/systemd/yantra-sandbox.service" \
    && fail "the DOOR reads the service_role env file -- it must stay on the anon key" \
    || pass "yantra-sandbox (the door) does not read the service_role env file"

# The marketing page and the door must agree on the route strings.
grep -q 'ROUTE_MINT = "/api/demo/session"' "$REPO_ROOT/ops/yantraops/sandbox_http.py" \
    && pass "sandbox_http.py still serves /api/demo/session (matches the nginx block + the button)" \
    || fail "sandbox_http.py's mint route moved -- nginx and marketing/index.html now point at nothing"
grep -q 'ROUTE_LIMITS = "/api/demo/limits"' "$REPO_ROOT/ops/yantraops/sandbox_http.py" \
    && pass "sandbox_http.py still serves /api/demo/limits (gates the button's visibility)" \
    || fail "sandbox_http.py's limits route moved -- the button would never appear"
grep -q 'action="/api/demo/session"' "$REPO_ROOT/marketing/index.html" \
    && pass "marketing/index.html posts to /api/demo/session with a real <form> (works with JS off)" \
    || fail "marketing/index.html has no zero-JS <form> posting to /api/demo/session"

# The conformance page's filename is baked into every report yantra-conform
# generates (report_html.py TOOL_URL), so renaming the file silently breaks
# the footer link in every report already sitting in someone's inbox.
[ -f "$REPO_ROOT/marketing/vda-5050-conformance-test.html" ] \
    && pass "marketing/vda-5050-conformance-test.html exists (hard-coded in generated reports)" \
    || fail "marketing/vda-5050-conformance-test.html missing -- generated reports link to it"
grep -q 'vda-5050-conformance-test.html' "$REPO_ROOT/tools/conformance/yantraconform/report_html.py" \
    && pass "report_html.py's footer link still matches that filename" \
    || fail "report_html.py no longer links vda-5050-conformance-test.html (filename drifted)"

# ---------------------------------------------------------------------------
# 5) regression check: user-data.sh must be pure ASCII. Same reasoning as
#    the azure kit's check — cloud-init/user-data scripts get base64'd
#    into cloud-provider API request bodies, and some Windows Python HTTP
#    stacks default to a latin-1 encoding that chokes on a stray em dash.
#    Pinned so it can't quietly regress here too.
# ---------------------------------------------------------------------------
if LC_ALL=C grep -qP '[^\x00-\x7F]' "$HERE/user-data.sh"; then
    fail "user-data.sh contains non-ASCII byte(s) (risks the same latin-1 crash the azure kit hit)"
else
    pass "user-data.sh is pure ASCII"
fi

# ---------------------------------------------------------------------------
echo
if [ "$FAILS" -eq 0 ]; then
    echo "validate.sh: all checks passed"
else
    echo "validate.sh: $FAILS check(s) FAILED" >&2
    exit 1
fi
