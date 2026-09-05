#!/usr/bin/env bash
#
# YantraFleet - EC2 user-data for Ubuntu 24.04 LTS.
#
# Paste this whole file into the AWS console:
#   EC2 -> Launch instance -> Advanced details -> User data
# after filling in the "EDIT ME" section below. It runs once as root on
# first boot (and is safe to re-run by hand later:
#   sudo bash /var/lib/cloud/instance/user-data.txt ).
#
# What it does: installs git/python/nginx, clones the repo to
# /opt/yantrafleet, creates a venv via install.sh, writes
# /etc/yantrafleet.env, installs the yantra-* systemd services, and
# templates + enables the nginx site that serves the console and proxies
# the sarathi copilot API.

# ============================= === EDIT ME === =============================
# 1) Your repo. For a PRIVATE GitHub repo, create a fine-grained personal
#    access token (github.com -> Settings -> Developer settings -> Tokens,
#    repo scope "Contents: read-only") and embed it in the URL:
#      REPO_URL="https://<YOUR_TOKEN>@github.com/<you>/yantrafleet.git"
#    For a public repo the plain https URL is enough.
REPO_URL="https://github.com/YOUR_GITHUB_USERNAME/yantrafleet.git"

# 2) Your Supabase project (dashboard -> Settings -> API). Use the
#    *publishable/anon* key here - it is embedded in the console URL and
#    therefore visible to anyone who can open the page.
SUPABASE_URL="https://YOUR_PROJECT_REF.supabase.co"
SUPABASE_KEY="YOUR_SUPABASE_PUBLISHABLE_ANON_KEY"

# 3) Which site this deployment serves (stamped on rows, filters alerts).
YANTRA_SITE_ID="BLR-DC1"

# 3b) Optional. Set BOTH to get HTTPS automatically at first boot instead
#     of the manual certbot step in the hardening checklist. DOMAIN_NAME
#     must already have a DNS A/AAAA record pointing at this instance's
#     public IP *before* it boots (Let's Encrypt validates over the
#     network) - if it doesn't yet, leave these blank and run the same
#     certbot command by hand once DNS is live (see README "Hardening
#     checklist"). CERTBOT_EMAIL is used only for Let's Encrypt renewal/
#     expiry notices, never shown publicly.
DOMAIN_NAME=""
CERTBOT_EMAIL=""

# 4) Optional. GEMINI_API_KEY enables the sarathi copilot's LLM tiers
#    (leave empty for the offline tier). SARATHI_TOKEN, when set, makes
#    /ask require "Authorization: Bearer <token>" - recommended once the
#    box is on the public internet.
GEMINI_API_KEY=""
SARATHI_TOKEN=""

# 5) Optional. Notifier fan-out channels (yantranotify) - leave any of
#    these empty to skip that channel; the notifier always still prints
#    to the console/journal. WEBHOOK_URL: a Slack "Incoming Webhook" or
#    Discord channel webhook URL. YANTRA_WEBHOOK_SECRET: if set, webhook
#    posts are HMAC-SHA256 signed (X-Yantra-Signature) so the receiver
#    can verify authenticity. TWILIO_*: WhatsApp alerts via Twilio (SID,
#    auth token, and the Twilio-provided "from"/your verified "to"
#    numbers, e.g. "whatsapp:+14155238886").
WEBHOOK_URL=""
YANTRA_WEBHOOK_SECRET=""
TWILIO_SID=""
TWILIO_TOKEN=""
TWILIO_FROM=""
TWILIO_TO=""
# =========================== === END EDIT ME === ===========================

set -euo pipefail
exec > >(tee -a /var/log/yantrafleet-install.log) 2>&1
echo "== yantrafleet user-data starting: $(date -Is)"

export DEBIAN_FRONTEND=noninteractive
APP_DIR=/opt/yantrafleet
ENV_FILE=/etc/yantrafleet.env

# ---------------------------------------------------------------------------
# 0) Refuse to run with the placeholders still in place
# ---------------------------------------------------------------------------
case "$REPO_URL$SUPABASE_URL$SUPABASE_KEY" in
  *YOUR_GITHUB_USERNAME*|*YOUR_PROJECT_REF*|*YOUR_SUPABASE_PUBLISHABLE*)
    echo "ERROR: fill in the '=== EDIT ME ===' section first (REPO_URL /"
    echo "SUPABASE_URL / SUPABASE_KEY still contain placeholders)." >&2
    exit 1;;
esac

# ---------------------------------------------------------------------------
# 1) OS packages (git, venv-capable python, nginx, envsubst)
# ---------------------------------------------------------------------------
echo "== installing OS packages"
apt-get update -y
apt-get install -y git python3.12-venv nginx gettext-base

# ---------------------------------------------------------------------------
# 2) Service user
# ---------------------------------------------------------------------------
if ! id -u yantra >/dev/null 2>&1; then
    echo "== creating service user 'yantra'"
    useradd --system --create-home --home-dir /home/yantra \
            --shell /usr/sbin/nologin yantra
fi

# ---------------------------------------------------------------------------
# 3) Clone (or update) the repo
# ---------------------------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
    echo "== $APP_DIR exists; pulling latest"
    git -C "$APP_DIR" pull --ff-only || echo "WARN: git pull failed; keeping current checkout"
else
    echo "== cloning $APP_DIR"
    git clone "$REPO_URL" "$APP_DIR"
fi
chown -R yantra:yantra "$APP_DIR"

# ---------------------------------------------------------------------------
# 4) Python venv + editable installs (the repo's own installer)
# ---------------------------------------------------------------------------
echo "== running install.sh as user 'yantra' (venv at $APP_DIR/.venv)"
if [ -f "$APP_DIR/install.sh" ]; then
    sudo -u yantra INSTALL_VENV_DIR="$APP_DIR/.venv" bash "$APP_DIR/install.sh"
else
    # Fallback if install.sh ever disappears: equivalent manual install.
    sudo -u yantra python3 -m venv "$APP_DIR/.venv"
    sudo -u yantra "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
    sudo -u yantra "$APP_DIR/.venv/bin/pip" install --quiet \
        -e "$APP_DIR/core" -e "$APP_DIR/sim" -e "$APP_DIR/connector" \
        -e "$APP_DIR/detector" -e "$APP_DIR/notifier" -e "$APP_DIR/ops"
    sudo -u yantra "$APP_DIR/.venv/bin/pip" install --quiet \
        -r "$APP_DIR/copilot/requirements.txt"
fi

# ---------------------------------------------------------------------------
# 5) Environment file the services read
# ---------------------------------------------------------------------------
echo "== writing $ENV_FILE"
umask 027
cat > "$ENV_FILE" <<EOF
SUPABASE_URL=$SUPABASE_URL
SUPABASE_KEY=$SUPABASE_KEY
YANTRA_SITE_ID=$YANTRA_SITE_ID
GEMINI_API_KEY=$GEMINI_API_KEY
SARATHI_TOKEN=$SARATHI_TOKEN
WEBHOOK_URL=$WEBHOOK_URL
YANTRA_WEBHOOK_SECRET=$YANTRA_WEBHOOK_SECRET
TWILIO_SID=$TWILIO_SID
TWILIO_TOKEN=$TWILIO_TOKEN
TWILIO_FROM=$TWILIO_FROM
TWILIO_TO=$TWILIO_TO
EOF
umask 022
chown root:yantra "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ---------------------------------------------------------------------------
# 6) systemd services
# ---------------------------------------------------------------------------
echo "== installing systemd units"
install -m 644 "$APP_DIR"/deploy/aws/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now yantra-detect yantra-notify yantra-sarathi
# yantra-sim is installed but NOT enabled: turn it on for a demo fleet with
#   sudo systemctl enable --now yantra-sim

# ---------------------------------------------------------------------------
# 7) nginx: console + academy + docs + copilot proxy
# ---------------------------------------------------------------------------
echo "== templating nginx site"
NGINX_SERVER_NAME="${DOMAIN_NAME:-_}"
export SUPABASE_URL SUPABASE_KEY YANTRA_SITE_ID NGINX_SERVER_NAME
envsubst '${SUPABASE_URL} ${SUPABASE_KEY} ${YANTRA_SITE_ID} ${NGINX_SERVER_NAME}' \
    < "$APP_DIR/deploy/aws/nginx/yantrafleet.conf.template" \
    > /etc/nginx/sites-available/yantrafleet
ln -sf /etc/nginx/sites-available/yantrafleet /etc/nginx/sites-enabled/yantrafleet
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

# ---------------------------------------------------------------------------
# 8) HTTPS via Let's Encrypt (only when both DOMAIN_NAME and CERTBOT_EMAIL
#    are set in step 1, and DOMAIN_NAME's DNS already points at this
#    instance's public IP -- same prerequisite the hardening checklist's
#    manual certbot step always had; this just runs it automatically at
#    boot instead of requiring a copy/paste command afterwards. A failure
#    here (usually DNS not propagated yet) is a WARN, not a fatal error --
#    the site keeps serving plain HTTP and the same command can be re-run
#    by hand once DNS is live.
# ---------------------------------------------------------------------------
if [ -n "$DOMAIN_NAME" ] && [ -n "$CERTBOT_EMAIL" ]; then
    echo "== requesting a Let's Encrypt certificate for $DOMAIN_NAME"
    apt-get install -y certbot python3-certbot-nginx
    if certbot --nginx --non-interactive --agree-tos -m "$CERTBOT_EMAIL" \
            -d "$DOMAIN_NAME" --redirect; then
        echo "== HTTPS live at https://$DOMAIN_NAME/ (certbot.timer handles renewal)"
    else
        echo "WARN: certbot failed for $DOMAIN_NAME -- most likely its DNS" >&2
        echo "A/AAAA record isn't pointing at this instance's public IP yet." >&2
        echo "The site is still reachable over plain HTTP. Fix DNS, then re-run:" >&2
        echo "  sudo certbot --nginx -d $DOMAIN_NAME -m $CERTBOT_EMAIL --redirect" >&2
    fi
elif [ -n "$DOMAIN_NAME" ] || [ -n "$CERTBOT_EMAIL" ]; then
    echo "WARN: DOMAIN_NAME and CERTBOT_EMAIL must both be set to enable" >&2
    echo "automatic HTTPS -- only one was provided, so skipping certbot." >&2
    echo "(the site is fine over plain HTTP; fill in both to enable this)" >&2
fi

echo "== yantrafleet user-data done: $(date -Is)"
if [ -n "$DOMAIN_NAME" ]; then
    echo "== open http://$DOMAIN_NAME/ (or https:// once certbot succeeds) in a browser"
else
    echo "== open http://<this instance's public IP>/ in a browser"
fi
