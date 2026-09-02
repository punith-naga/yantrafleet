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

if command -v envsubst >/dev/null 2>&1; then
    RENDERED="$(envsubst '${SUPABASE_URL} ${SUPABASE_KEY} ${YANTRA_SITE_ID}' < "$TEMPLATE")"
    pass "envsubst dry-run of nginx template"
else
    # envsubst (gettext-base) missing locally — same substitution in python.
    RENDERED="$(python3 - "$TEMPLATE" <<'PY'
import os, sys
text = open(sys.argv[1]).read()
for var in ("SUPABASE_URL", "SUPABASE_KEY", "YANTRA_SITE_ID"):
    text = text.replace("${%s}" % var, os.environ[var])
print(text)
PY
)"
    pass "nginx template dry-run (python fallback; envsubst not installed here)"
fi

echo "$RENDERED" | grep -q 'supa=https://dummy.supabase.co&key=dummy-anon-key&site=TEST-SITE' \
    && pass "302 redirect carries supa/key/site" \
    || fail "302 redirect missing substituted supa/key/site"
echo "$RENDERED" | grep -q '${SUPABASE\|${YANTRA' \
    && fail "unsubstituted \${...} placeholders remain in rendered nginx conf" \
    || pass "no leftover \${...} placeholders"
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
    for unit in "${UNITS[@]}"; do
        name="$(basename "$unit")"
        ok=1
        for required in '^\[Unit\]' '^\[Service\]' '^\[Install\]' \
                        '^Description=' '^ExecStart=/opt/yantrafleet/\.venv/bin/python' \
                        '^EnvironmentFile=/etc/yantrafleet\.env' \
                        '^Restart=always' '^User=yantra' \
                        '^WantedBy=multi-user\.target'; do
            grep -q "$required" "$unit" || { fail "$name: missing '$required'"; ok=0; }
        done
        # no stray keys outside a section; every non-blank/comment line is
        # either a [Section] or key=value
        grep -vE '^\s*(#|$|\[[A-Za-z]+\]|[A-Za-z][A-Za-z0-9]*=)' "$unit" \
            | grep -q . && { fail "$name: malformed line(s)"; ok=0; }
        [ "$ok" -eq 1 ] && pass "unit lint $name"
    done
fi

# sarathi must bind loopback:8001 (the nginx proxy target)
grep -q -- '--host 127.0.0.1 --port 8001' "$HERE/systemd/yantra-sarathi.service" \
    && pass "yantra-sarathi binds 127.0.0.1:8001" \
    || fail "yantra-sarathi does not bind 127.0.0.1:8001"

# ---------------------------------------------------------------------------
echo
if [ "$FAILS" -eq 0 ]; then
    echo "validate.sh: all checks passed"
else
    echo "validate.sh: $FAILS check(s) FAILED" >&2
    exit 1
fi
