#!/usr/bin/env bash
#
# One-command enabler for the zero-signup "Try it with a live fleet" demo
# door. Wraps the "Turning on the zero-signup live demo" steps from
# deploy/aws/README.md (and deploy/azure/README.md — same box layout, same
# systemd units) into a single script instead of five manual steps spread
# across two files.
#
# It is deliberately NOT run automatically at boot: the demo door hands
# anonymous internet visitors write access (scoped to one throwaway site —
# see supabase/0009_demo_sandbox.sql's THREAT MODEL block), so turning it on
# stays an explicit, one-time operator decision. This script makes that one
# decision fast instead of making the mechanics of it slow.
#
# Usage (run on the deployed box, as root):
#   sudo ./enable-demo.sh --db-url "postgresql://postgres:<pw>@<host>:5432/postgres" \
#                          --service-role-key "<service_role key from Supabase dashboard>"
#
#   sudo ./enable-demo.sh --skip-migrate --service-role-key "..."
#     (use this if 0007/0009/0017 are already applied — skips the migrate step)
#
set -euo pipefail

APP_DIR=/opt/yantrafleet
VENV_PY="$APP_DIR/.venv/bin/python"
SANDBOX_ENV=/etc/yantrafleet-sandbox.env

usage() {
  cat <<'EOF'
Usage: sudo ./enable-demo.sh --service-role-key "<key>" [--db-url "postgresql://..."] [--skip-migrate]

  --service-role-key   Required. Supabase Settings -> API -> service_role
                        (secret) key. Used only by the reaper unit, which
                        needs it to delete expired sandboxes across sites —
                        the door itself keeps using the anon key.
  --db-url              Postgres connection string with migration privilege
                        (same one 'yantraops migrate' takes). Required
                        unless --skip-migrate is given.
  --skip-migrate         Assume supabase/0007, 0009 and 0017 are already
                        applied to your project; only write the reaper env
                        file and enable the systemd units.
EOF
  exit 1
}

DB_URL=""
SERVICE_ROLE_KEY=""
SKIP_MIGRATE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --db-url) DB_URL="${2:-}"; shift 2 ;;
    --service-role-key) SERVICE_ROLE_KEY="${2:-}"; shift 2 ;;
    --skip-migrate) SKIP_MIGRATE=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo)." >&2; exit 1; }
[ -n "$SERVICE_ROLE_KEY" ] || { echo "ERROR: --service-role-key is required." >&2; usage; }
[ -x "$VENV_PY" ] || { echo "ERROR: $VENV_PY not found — is this the deployed app box (AWS or Azure kit)?" >&2; exit 1; }

if [ "$SKIP_MIGRATE" -eq 0 ]; then
  [ -n "$DB_URL" ] || { echo "ERROR: --db-url is required unless --skip-migrate is given." >&2; usage; }
  echo "== reading supabase/0009_demo_sandbox.sql's THREAT MODEL block is on you — this script only automates applying it, not understanding it"
  echo "== applying opt-in migrations 0006-0017 (includes 0007 RBAC, required for the demo sandbox's isolation guarantees to actually hold,"
  echo "   0009 the sandbox itself, and 0017 which tightens 0009's write scope)"
  sudo -u yantra "$VENV_PY" -m yantraops migrate --db-url "$DB_URL" --include-opt-in
else
  echo "== --skip-migrate given: assuming 0007, 0009 and 0017 are already applied"
fi

echo "== writing $SANDBOX_ENV (mode 640, root:yantra — same protection as /etc/yantrafleet.env)"
umask 037
printf 'SUPABASE_KEY=%s\n' "$SERVICE_ROLE_KEY" > "$SANDBOX_ENV"
umask 022
chown root:yantra "$SANDBOX_ENV"
chmod 640 "$SANDBOX_ENV"

echo "== enabling yantra-sandbox + yantra-sandbox-reap.timer"
systemctl daemon-reload
systemctl enable --now yantra-sandbox yantra-sandbox-reap.timer

echo "== verifying"
sleep 1
if curl -sf localhost:8088/healthz >/dev/null 2>&1; then
  echo "   sandbox door: healthy (localhost:8088/healthz)"
else
  echo "   WARNING: sandbox door did not answer on localhost:8088/healthz — check: journalctl -u yantra-sandbox -n 50 --no-pager" >&2
fi
if curl -sf localhost/api/demo/limits >/dev/null 2>&1; then
  echo "   /api/demo/limits: reachable through nginx"
else
  echo "   WARNING: /api/demo/limits not reachable through nginx — check the nginx site config and that port 80 is open" >&2
fi

echo "== done. The 'Try it with a live fleet' button on the marketing site should now work end to end."
echo "   Watch the reaper: systemctl list-timers yantra-sandbox-reap.timer"
