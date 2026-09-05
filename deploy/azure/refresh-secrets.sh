#!/usr/bin/env bash
#
# YantraFleet - manual secrets refresh for an Azure VM deployed with
# USE_KEYVAULT=true.
#
# custom-data.sh installs this to /opt/yantrafleet/refresh-secrets.sh on
# first boot. Run it later, as root, after rotating any secret in the Key
# Vault to re-pull the current values and restart the services that read
# them - without recreating the VM:
#
#   sudo /opt/yantrafleet/refresh-secrets.sh
#
# It is a self-contained copy of the IMDS + Key Vault fetch logic in
# custom-data.sh (not shared via a common file - this script has to keep
# working standalone on a running VM, long after cloud-init has finished,
# so duplicating the ~80 lines is simpler than wiring up a dependency
# between the two).

set -euo pipefail

ENV_FILE=/etc/yantrafleet.env

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run this as root (sudo /opt/yantrafleet/refresh-secrets.sh)." >&2
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found - this doesn't look like a yantrafleet VM." >&2
    exit 1
fi

# Read the existing env file so we have YANTRA_SITE_ID (non-secret, must be
# preserved) and KEYVAULT_NAME (non-secret, tells us where to fetch from).
# Everything else in it is about to be overwritten with freshly-fetched
# values, so there's no need to source the old secrets too.
KEYVAULT_NAME="$(grep -oP '^KEYVAULT_NAME=\K.*' "$ENV_FILE" || true)"
YANTRA_SITE_ID="$(grep -oP '^YANTRA_SITE_ID=\K.*' "$ENV_FILE" || true)"

if [ -z "$KEYVAULT_NAME" ]; then
    echo "ERROR: KEYVAULT_NAME is empty in $ENV_FILE - this deployment was not" >&2
    echo "set up with USE_KEYVAULT=true, so there is nothing to refresh from." >&2
    echo "Edit $ENV_FILE by hand instead and restart the services yourself." >&2
    exit 1
fi

echo "== refreshing secrets from Key Vault: $KEYVAULT_NAME"

# Same retry treatment as custom-data.sh's boot-time fetch: RBAC role
# assignments can lag by a minute or two, and this can legitimately be run
# again right after the vault's access policy changed.
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
    # Status-code-aware, matching custom-data.sh: a missing optional secret
    # is a permanent 404 (deploy.sh only uploads non-empty vars), not a
    # propagation-lag issue, so don't burn the full retry budget on it.
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
    echo "'Key Vault Secrets User' role assignment on $KEYVAULT_NAME." >&2
    exit 1
}

# SUPABASE_URL/SUPABASE_KEY are required - the console and copilot API
# can't run without them.
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

# REPO_URL (Key Vault secret "repo-url") is deliberately NOT refreshed
# here: it's only consumed once, at clone/pull time, by custom-data.sh and
# the "Updating to a new version" flow in README.md -- none of the
# services this script restarts read it, so there's nothing to feed it to.
# A rotated repo-url takes effect the next time you pull, not on refresh.
#
# Everything else is optional - same "leave it empty to skip that channel"
# behavior as custom-data.sh: a fetch failure just leaves it blank instead
# of failing the whole refresh.
GEMINI_API_KEY="$(kv_get_secret gemini-api-key "$KV_TOKEN")" || { GEMINI_API_KEY=""; echo "   .. gemini-api-key not in vault, leaving blank"; }
SARATHI_TOKEN="$(kv_get_secret sarathi-token "$KV_TOKEN")" || { SARATHI_TOKEN=""; echo "   .. sarathi-token not in vault, leaving blank"; }
WEBHOOK_URL="$(kv_get_secret webhook-url "$KV_TOKEN")" || { WEBHOOK_URL=""; echo "   .. webhook-url not in vault, leaving blank"; }
YANTRA_WEBHOOK_SECRET="$(kv_get_secret yantra-webhook-secret "$KV_TOKEN")" || { YANTRA_WEBHOOK_SECRET=""; echo "   .. yantra-webhook-secret not in vault, leaving blank"; }
TWILIO_SID="$(kv_get_secret twilio-sid "$KV_TOKEN")" || { TWILIO_SID=""; echo "   .. twilio-sid not in vault, leaving blank"; }
TWILIO_TOKEN="$(kv_get_secret twilio-token "$KV_TOKEN")" || { TWILIO_TOKEN=""; echo "   .. twilio-token not in vault, leaving blank"; }
TWILIO_FROM="$(kv_get_secret twilio-from "$KV_TOKEN")" || { TWILIO_FROM=""; echo "   .. twilio-from not in vault, leaving blank"; }
TWILIO_TO="$(kv_get_secret twilio-to "$KV_TOKEN")" || { TWILIO_TO=""; echo "   .. twilio-to not in vault, leaving blank"; }

echo "== secrets fetched, rewriting $ENV_FILE"
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

echo "== restarting services"
systemctl restart yantra-detect yantra-notify yantra-sarathi

echo "== done: $(date -Is)"
