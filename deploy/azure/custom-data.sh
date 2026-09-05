#!/usr/bin/env bash
#
# YantraFleet - Azure cloud-init custom-data for Ubuntu 24.04 LTS.
#
# Used by ../deploy.sh via `az vm create --custom-data`. Azure's cloud-init
# runs this once as root on first boot, exactly like AWS EC2 user-data (a
# script starting with `#!` is the same "x-shellscript" cloud-init part on
# both clouds), so this file is a straight adaptation of
# ../aws/user-data.sh - same EDIT ME block, same steps. It deliberately
# reuses the systemd units and nginx template under ../aws/ rather than
# duplicating them: those files are cloud-agnostic (plain systemd +
# nginx), only the VM-provisioning step differs between clouds.
#
# Safe to re-run by hand later:
#   sudo bash /var/lib/cloud/instance/scripts/part-001
#
# What it does: installs git/python/nginx/curl, optionally fetches secrets
# from Azure Key Vault via the VM's managed identity (see KEYVAULT_NAME
# below), clones the repo to /opt/yantrafleet, creates a venv via
# install.sh, writes /etc/yantrafleet.env, installs
# /opt/yantrafleet/refresh-secrets.sh for later rotation, installs the
# yantra-* systemd services, templates + enables the nginx site that
# serves the console and proxies the sarathi copilot API, and -- when
# DOMAIN_NAME + CERTBOT_EMAIL are set -- requests a Let's Encrypt cert via
# certbot's nginx plugin so the site comes up on HTTPS from first boot.

# ============================= === EDIT ME === =============================
# 1) Your repo. For a PRIVATE GitHub repo, create a fine-grained personal
#    access token (github.com -> Settings -> Developer settings -> Tokens,
#    repo scope "Contents: read-only") and embed it in the URL:
#      REPO_URL="https://<YOUR_TOKEN>@github.com/<you>/yantrafleet.git"
#    For a public repo the plain https URL is enough. When USE_KEYVAULT=true
#    (the default), deploy.sh uploads this whole value -- token included --
#    to Key Vault as "repo-url" and blanks this line before it ever reaches
#    Azure's control plane, same treatment as SUPABASE_URL/KEY below; the VM
#    fetches it back at boot. See supabase/README.md's "Going to production"
#    note -- REPO_URL is credential-shaped exactly like the others once a
#    private repo is in play, it just wasn't originally treated as one.
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
#     must already have a DNS A/AAAA record pointing at this VM's public IP
#     *before* it boots (Let's Encrypt validates over the network) - if it
#     doesn't yet, leave these blank and run the same certbot command by
#     hand once DNS is live (see README "Hardening checklist"). These are
#     not secrets, so they stay in this EDIT ME block even when
#     USE_KEYVAULT=true - only the 11 credential-shaped vars below go
#     through Key Vault.
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

# 6) Key Vault name (Azure only). Leave this empty - deploy.sh fills it in
#    automatically when USE_KEYVAULT=true (the default), so there is
#    nothing to edit here by hand. When empty (USE_KEYVAULT=false, or this
#    file run/edited directly outside deploy.sh) the Key Vault fetch below
#    is a no-op and the values above are used exactly as typed above.
KEYVAULT_NAME=""
# =========================== === END EDIT ME === ===========================

set -euo pipefail
exec > >(tee -a /var/log/yantrafleet-install.log) 2>&1
echo "== yantrafleet custom-data starting: $(date -Is)"

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
# 1) OS packages (git, venv-capable python, nginx, envsubst, curl for the
#    Key Vault fetch below)
# ---------------------------------------------------------------------------
echo "== installing OS packages"
apt-get update -y
apt-get install -y git python3.12-venv nginx gettext-base curl

# ---------------------------------------------------------------------------
# 2) Secrets from Key Vault (only when deploy.sh set USE_KEYVAULT=true and
#    filled in KEYVAULT_NAME above; otherwise this is a no-op and the
#    EDIT ME values from above pass straight through, same as before Key
#    Vault support existed)
# ---------------------------------------------------------------------------
if [ -n "$KEYVAULT_NAME" ]; then
    echo "== fetching secrets from Key Vault: $KEYVAULT_NAME"

    # The VM's managed identity is granted "Key Vault Secrets User" on this
    # vault by deploy.sh right after the VM is created, but RBAC role
    # assignments can take a minute or two to propagate - so both the IMDS
    # token fetch and each secret fetch retry on failure instead of giving
    # up after one shot. ~24 attempts * 5s = ~2 minutes.
    KV_MAX_ATTEMPTS=24
    KV_RETRY_DELAY=5

    kv_get_token() {
        local attempt=1 resp token
        while [ "$attempt" -le "$KV_MAX_ATTEMPTS" ]; do
            resp="$(curl -sf --max-time 10 -H "Metadata:true" \
                "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net" \
                2>/dev/null || true)"
            token="$(printf '%s' "$resp" | grep -oP '"access_token"\s*:\s*"\K[^"]+' || true)"
            if [ -n "$token" ]; then
                printf '%s' "$token"
                return 0
            fi
            echo "   .. IMDS token attempt $attempt/$KV_MAX_ATTEMPTS failed, retrying in ${KV_RETRY_DELAY}s"
            attempt=$((attempt + 1))
            sleep "$KV_RETRY_DELAY"
        done
        return 1
    }

    kv_get_secret() {
        # $1 = Key Vault secret name, $2 = access token
        #
        # Status-code-aware on purpose: deploy.sh only uploads the optional
        # vars that were actually non-empty, so a missing optional secret is
        # a permanent 404, not a propagation-lag issue - retrying that for
        # the full 2-minute budget (x6 possible optional secrets) turned a
        # documented "~5 minute" boot into 10+ minutes for a typical partial
        # config. Only retry on a non-200 that ISN'T a clean 404.
        local name="$1" token="$2" attempt=1 resp http_code body
        while [ "$attempt" -le "$KV_MAX_ATTEMPTS" ]; do
            resp="$(curl -s --max-time 10 -H "Authorization: Bearer $token" \
                -w '\n%{http_code}' \
                "https://${KEYVAULT_NAME}.vault.azure.net/secrets/${name}?api-version=7.4" \
                2>/dev/null || true)"
            http_code="${resp##*$'\n'}"
            body="${resp%$'\n'*}"
            if [ "$http_code" = "200" ] && printf '%s' "$body" | grep -q '"value"'; then
                printf '%s' "$body" | grep -oP '"value"\s*:\s*"\K[^"]*'
                return 0
            fi
            if [ "$http_code" = "404" ]; then
                return 1
            fi
            attempt=$((attempt + 1))
            sleep "$KV_RETRY_DELAY"
        done
        return 1
    }

    KV_TOKEN="$(kv_get_token)" || {
        echo "ERROR: could not obtain a Key Vault access token from IMDS after" >&2
        echo "$KV_MAX_ATTEMPTS attempts. Check the VM's managed identity and the" >&2
        echo "'Key Vault Secrets User' role assignment (see deploy.sh)." >&2
        exit 1
    }

    # REPO_URL/SUPABASE_URL/SUPABASE_KEY are required - there's no repo to
    # clone or backend to talk to without them, same as today's placeholder
    # guard above.
    REPO_URL="$(kv_get_secret repo-url "$KV_TOKEN")" || {
        echo "ERROR: could not fetch secret 'repo-url' from Key Vault" \
             "$KEYVAULT_NAME after $KV_MAX_ATTEMPTS attempts." >&2
        exit 1
    }
    SUPABASE_URL="$(kv_get_secret supabase-url "$KV_TOKEN")" || {
        echo "ERROR: could not fetch secret 'supabase-url' from Key Vault" \
             "$KEYVAULT_NAME after $KV_MAX_ATTEMPTS attempts." >&2
        exit 1
    }
    SUPABASE_KEY="$(kv_get_secret supabase-key "$KV_TOKEN")" || {
        echo "ERROR: could not fetch secret 'supabase-key' from Key Vault" \
             "$KEYVAULT_NAME after $KV_MAX_ATTEMPTS attempts." >&2
        exit 1
    }

    # Everything else is optional - same "leave it empty to skip that
    # channel" behavior as the hand-edited EDIT ME block: a fetch failure
    # just leaves the (already blank) default in place instead of failing
    # the whole boot.
    GEMINI_API_KEY="$(kv_get_secret gemini-api-key "$KV_TOKEN")" || echo "   .. gemini-api-key not in vault, leaving blank"
    SARATHI_TOKEN="$(kv_get_secret sarathi-token "$KV_TOKEN")" || echo "   .. sarathi-token not in vault, leaving blank"
    WEBHOOK_URL="$(kv_get_secret webhook-url "$KV_TOKEN")" || echo "   .. webhook-url not in vault, leaving blank"
    YANTRA_WEBHOOK_SECRET="$(kv_get_secret yantra-webhook-secret "$KV_TOKEN")" || echo "   .. yantra-webhook-secret not in vault, leaving blank"
    TWILIO_SID="$(kv_get_secret twilio-sid "$KV_TOKEN")" || echo "   .. twilio-sid not in vault, leaving blank"
    TWILIO_TOKEN="$(kv_get_secret twilio-token "$KV_TOKEN")" || echo "   .. twilio-token not in vault, leaving blank"
    TWILIO_FROM="$(kv_get_secret twilio-from "$KV_TOKEN")" || echo "   .. twilio-from not in vault, leaving blank"
    TWILIO_TO="$(kv_get_secret twilio-to "$KV_TOKEN")" || echo "   .. twilio-to not in vault, leaving blank"

    echo "== secrets fetched from Key Vault"
fi

# ---------------------------------------------------------------------------
# 3) Service user
# ---------------------------------------------------------------------------
if ! id -u yantra >/dev/null 2>&1; then
    echo "== creating service user 'yantra'"
    useradd --system --create-home --home-dir /home/yantra \
            --shell /usr/sbin/nologin yantra
fi

# ---------------------------------------------------------------------------
# 4) Clone (or update) the repo
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
# 5) Python venv + editable installs (the repo's own installer)
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
# 6) Environment file the services read
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
KEYVAULT_NAME=$KEYVAULT_NAME
EOF
umask 022
chown root:yantra "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ---------------------------------------------------------------------------
# 7) refresh-secrets.sh (lets an operator re-pull rotated secrets later
#    without recreating the VM - harmless no-op if KEYVAULT_NAME is empty)
# ---------------------------------------------------------------------------
echo "== installing /opt/yantrafleet/refresh-secrets.sh"
install -m 755 -o root -g root "$APP_DIR/deploy/azure/refresh-secrets.sh" \
    /opt/yantrafleet/refresh-secrets.sh

# ---------------------------------------------------------------------------
# 8) systemd services (shared units - same files the AWS kit installs)
# ---------------------------------------------------------------------------
echo "== installing systemd units"
install -m 644 "$APP_DIR"/deploy/aws/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now yantra-detect yantra-notify yantra-sarathi
# yantra-sim is installed but NOT enabled: turn it on for a demo fleet with
#   sudo systemctl enable --now yantra-sim

# ---------------------------------------------------------------------------
# 9) nginx: console + academy + docs + copilot proxy (shared template)
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
# 10) HTTPS via Let's Encrypt (only when both DOMAIN_NAME and CERTBOT_EMAIL
#     are set in the EDIT ME block, and DOMAIN_NAME's DNS already points at
#     this VM's public IP -- same prerequisite the hardening checklist's
#     manual certbot step always had; this just runs it automatically at
#     boot instead of requiring a copy/paste command afterwards. A failure
#     here (usually DNS not propagated yet) is a WARN, not fatal -- the
#     site keeps serving plain HTTP and the same command can be re-run by
#     hand once DNS is live.
# ---------------------------------------------------------------------------
if [ -n "$DOMAIN_NAME" ] && [ -n "$CERTBOT_EMAIL" ]; then
    echo "== requesting a Let's Encrypt certificate for $DOMAIN_NAME"
    apt-get install -y certbot python3-certbot-nginx
    if certbot --nginx --non-interactive --agree-tos -m "$CERTBOT_EMAIL" \
            -d "$DOMAIN_NAME" --redirect; then
        echo "== HTTPS live at https://$DOMAIN_NAME/ (certbot.timer handles renewal)"
    else
        echo "WARN: certbot failed for $DOMAIN_NAME -- most likely its DNS" >&2
        echo "A/AAAA record isn't pointing at this VM's public IP yet." >&2
        echo "The site is still reachable over plain HTTP. Fix DNS, then re-run:" >&2
        echo "  sudo certbot --nginx -d $DOMAIN_NAME -m $CERTBOT_EMAIL --redirect" >&2
    fi
elif [ -n "$DOMAIN_NAME" ] || [ -n "$CERTBOT_EMAIL" ]; then
    echo "WARN: DOMAIN_NAME and CERTBOT_EMAIL must both be set to enable" >&2
    echo "automatic HTTPS -- only one was provided, so skipping certbot." >&2
    echo "(the site is fine over plain HTTP; fill in both to enable this)" >&2
fi

echo "== yantrafleet custom-data done: $(date -Is)"
if [ -n "$DOMAIN_NAME" ]; then
    echo "== marketing site: http://$DOMAIN_NAME/ (or https:// once certbot succeeds)"
    echo "== console: http://$DOMAIN_NAME/console/"
else
    echo "== marketing site: http://<this VM's public IP>/"
    echo "== console: http://<this VM's public IP>/console/"
fi
