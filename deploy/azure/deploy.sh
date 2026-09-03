#!/usr/bin/env bash
# YantraFleet — Azure deploy (CLI, not portal-click, by design: see
# deploy/azure/README.md for why). Creates one resource group, one Ubuntu
# VM with cloud-init custom-data (the EDIT ME'd custom-data.sh next to
# this file), opens port 80, prints the public IP. Idempotent-ish: safe
# to re-run (az vm create fails loudly if the VM already exists rather
# than doing anything destructive).
#
# Prereqs:
#   1. Azure CLI installed (https://aka.ms/installazurecliwindows, or
#      `winget install -e --id Microsoft.AzureCLI`).
#   2. `az login` run once in your own terminal (opens your own browser;
#      nothing is shared with anyone else).
#   3. deploy/azure/custom-data.sh has its "EDIT ME" block filled in
#      (REPO_URL / SUPABASE_URL / SUPABASE_KEY at minimum).
#
# Usage:
#   cd deploy/azure
#   ./deploy.sh                # uses the defaults below
#   RG=myrg LOCATION=eastus SIZE=Standard_B2s ./deploy.sh   # pin a single region/size
#   LOCATIONS="indiasouthcentral westindia" ./deploy.sh      # override the fallback order
#
# LOCATION-fallback behaviour: az vm create can fail per-region for two
# unrelated reasons, and this script now retries past both instead of
# making you manually re-run with a new LOCATION each time (both hit a
# live deploy attempt of this exact kit):
#   - SkuNotAvailable ("failed for Capacity Restrictions") — the region
#     is simply out of stock for that VM size right now. Transient,
#     nothing wrong with your account.
#   - DisallowedLocation — this Azure subscription isn't permitted to
#     deploy to that region at all (subscription-type/policy restriction,
#     e.g. some regions are gated behind a support request). Also not a
#     mistake on your part — just skip it.
# Any OTHER failure (bad image, RBAC, malformed custom-data, real quota
# exhaustion, ...) stops immediately with the full error rather than
# being silently retried across the rest of the list.
# Set LOCATION to pin exactly one region (skips the fallback list
# entirely); otherwise LOCATIONS (space-separated) is tried in order.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RG="${RG:-yantrafleet-rg}"
VM_NAME="${VM_NAME:-yantrafleet}"
ADMIN_USER="${ADMIN_USER:-yantra}"
IMAGE="${IMAGE:-Canonical:ubuntu-24_04-lts:server:latest}"
SIZE="${SIZE:-Standard_B2s}"       # 2 vCPU / 4 GiB — comfortably runs all services
DISK_SIZE_GB="${DISK_SIZE_GB:-30}"

# USE_KEYVAULT: when true (the default), the 10 credential-shaped vars in
# custom-data.sh's EDIT ME block (SUPABASE_URL/KEY, GEMINI_API_KEY,
# SARATHI_TOKEN, WEBHOOK_URL, YANTRA_WEBHOOK_SECRET, TWILIO_*) get uploaded
# to an Azure Key Vault instead of being handed to Azure as plaintext
# custom-data -- custom_data on a VM resource sits in Azure's control plane
# forever (readable via Portal/API/ARM export by anyone with Reader on the
# VM), not just briefly at boot, so leaving secrets in it is a standing
# leak, not a one-time risk. The VM fetches them back at boot via its own
# managed identity (see custom-data.sh). Set USE_KEYVAULT=false for today's
# exact behaviour instead (custom-data.sh passed through verbatim, no
# vault/identity/role-assignment work at all) -- e.g. a subscription
# without Key Vault permissions, or a quick local test.
USE_KEYVAULT="${USE_KEYVAULT:-true}"

# Key Vault mode builds a temp custom-data copy (secrets blanked,
# KEYVAULT_NAME filled in) and hands that to az vm create instead of
# custom-data.sh itself -- cleaned up on exit no matter how we get there.
CUSTOM_DATA_FILE="custom-data.sh"
TMP_CUSTOM_DATA=""
cleanup_tmp_custom_data() { [ -n "$TMP_CUSTOM_DATA" ] && rm -f "$TMP_CUSTOM_DATA"; }
trap cleanup_tmp_custom_data EXIT

if [ -n "${LOCATION:-}" ]; then
    LOCATIONS="$LOCATION"          # a single explicit LOCATION disables fallback
else
    # India regions first (latency), then well-stocked regions worldwide.
    LOCATIONS="${LOCATIONS:-centralindia indiasouthcentral westindia southeastasia eastus2 uksouth centralus}"
fi

if ! command -v az >/dev/null 2>&1; then
    echo "ERROR: Azure CLI ('az') not found. Install it first:" >&2
    echo "  winget install -e --id Microsoft.AzureCLI   (Windows)" >&2
    echo "  https://learn.microsoft.com/cli/azure/install-azure-cli" >&2
    exit 1
fi

if ! az account show >/dev/null 2>&1; then
    echo "ERROR: not logged in. Run 'az login' first (opens your browser," >&2
    echo "you approve, done — this script never sees your Azure password)." >&2
    exit 1
fi

if grep -E '^(REPO_URL|SUPABASE_URL|SUPABASE_KEY)=' custom-data.sh \
        | grep -q 'YOUR_GITHUB_USERNAME\|YOUR_PROJECT_REF\|YOUR_SUPABASE_PUBLISHABLE'; then
    echo "ERROR: custom-data.sh still has placeholders in its EDIT ME block." >&2
    echo "Open deploy/azure/custom-data.sh and fill in REPO_URL / SUPABASE_URL /" >&2
    echo "SUPABASE_KEY (and optionally the Gemini/Sarathi/notifier vars) first." >&2
    exit 1
fi

echo "== subscription in use:"
az account show --query '{name:name, id:id}' -o table

# RG_LOCATION only matters the first time the RG is created (az group
# create is a no-op — and ignores --location — if it already exists); it
# doesn't have to match where the VM itself ends up.
RG_LOCATION="${LOCATIONS%% *}"
echo "== creating resource group '$RG' in '$RG_LOCATION' (no-op if it exists)"
az group create --name "$RG" --location "$RG_LOCATION" -o table

VM_CREATE_EXTRA_ARGS=()
KEYVAULT_NAME="${KEYVAULT_NAME:-}"

if [ "$USE_KEYVAULT" = "true" ]; then
    # ------------------------------------------------------------------
    # Key Vault: create it (or reuse an existing one), grant ourselves
    # upload rights, push the 10 secret-shaped vars into it, then build
    # a temp custom-data copy with those vars blanked and KEYVAULT_NAME
    # filled in -- that temp copy is what az vm create gets below, not
    # custom-data.sh itself (which would defeat the whole point: the
    # plaintext values are still sitting in its EDIT ME block).
    # ------------------------------------------------------------------
    KEYVAULT_NAME_WAS_SET="false"
    if [ -n "$KEYVAULT_NAME" ]; then
        KEYVAULT_NAME_WAS_SET="true"
    else
        KV_BASE="$(printf '%s' "${VM_NAME}-kv" | tr '_' '-' | tr -cd 'a-zA-Z0-9-')"
        KV_BASE="${KV_BASE:0:24}"
        KEYVAULT_NAME="$KV_BASE"
    fi

    echo "== checking for an existing Key Vault named '$KEYVAULT_NAME'"
    if az keyvault show --name "$KEYVAULT_NAME" -o none 2>/dev/null; then
        echo "   found -- reusing it (already owned by this subscription)"
    else
        echo "== creating Key Vault '$KEYVAULT_NAME' in '$RG_LOCATION' (RBAC-authorized)"
        KV_OUT="$(mktemp)"
        if az keyvault create \
            --name "$KEYVAULT_NAME" \
            --resource-group "$RG" \
            --location "$RG_LOCATION" \
            --enable-rbac-authorization true \
            -o table > "$KV_OUT" 2>&1
        then
            cat "$KV_OUT"; rm -f "$KV_OUT"
        elif [ "$KEYVAULT_NAME_WAS_SET" = "false" ] \
                && grep -qi 'already exists\|already in use\|VaultAlreadyExists' "$KV_OUT"; then
            # Key Vault names are globally unique across every Azure tenant,
            # so a derived name can collide with someone else's vault --
            # retry once with a random suffix. An explicit KEYVAULT_NAME
            # override is left alone here so a collision fails loudly
            # instead of silently deploying under a different name.
            rm -f "$KV_OUT"
            SUFFIX="$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
            KEYVAULT_NAME="${KV_BASE:0:20}${SUFFIX}"
            echo "   name taken globally -- retrying once as '$KEYVAULT_NAME'"
            az keyvault create \
                --name "$KEYVAULT_NAME" \
                --resource-group "$RG" \
                --location "$RG_LOCATION" \
                --enable-rbac-authorization true \
                -o table
        else
            cat "$KV_OUT" >&2
            rm -f "$KV_OUT"
            echo "ERROR: az keyvault create failed for a reason other than the name" >&2
            echo "being taken (see output above)." >&2
            exit 1
        fi
    fi

    KV_ID="$(az keyvault show --name "$KEYVAULT_NAME" --query id -o tsv)"

    echo "== granting yourself 'Key Vault Secrets Officer' on '$KEYVAULT_NAME'"
    echo "   (an RBAC-authorized vault gives its creator no implicit access,"
    echo "   unlike legacy access-policy vaults -- this is what lets the"
    echo "   secret uploads below succeed at all)"
    CALLER_ID="$(az ad signed-in-user show --query id -o tsv)"
    SELF_GRANT_OUT="$(az role assignment create \
        --assignee-object-id "$CALLER_ID" \
        --assignee-principal-type User \
        --role "Key Vault Secrets Officer" \
        --scope "$KV_ID" -o none 2>&1)" || {
        if printf '%s' "$SELF_GRANT_OUT" | grep -qi 'RoleAssignmentExists\|already exists'; then
            echo "   already granted (re-run) -- continuing"
        else
            echo "$SELF_GRANT_OUT" >&2
            echo "ERROR: could not grant yourself Key Vault access." >&2
            exit 1
        fi
    }

    # RBAC propagation can lag ~1-2 minutes even for the grantor, so secret
    # uploads need a retry-with-backoff wrapper -- poll on the actual
    # Forbidden/AuthorizationFailed error, not a fixed sleep.
    kv_secret_set_with_retry() {
        local secret_name="$1" secret_value="$2" attempt out
        for attempt in $(seq 1 24); do
            if out="$(az keyvault secret set --vault-name "$KEYVAULT_NAME" \
                    --name "$secret_name" --value "$secret_value" -o none 2>&1)"; then
                return 0
            fi
            if printf '%s' "$out" | grep -qi 'Forbidden\|AuthorizationFailed'; then
                echo "   RBAC not propagated yet for '$secret_name' (attempt $attempt/24) -- waiting 5s"
                sleep 5
                continue
            fi
            echo "$out" >&2
            echo "ERROR: az keyvault secret set failed for '$secret_name' for a reason" >&2
            echo "other than RBAC propagation lag (see output above)." >&2
            return 1
        done
        echo "ERROR: '$secret_name' still failing with Forbidden/AuthorizationFailed" >&2
        echo "after 2 minutes of retries -- check the role assignment above." >&2
        return 1
    }

    # Pull each var's current value straight off its own line in
    # custom-data.sh (not by sourcing the whole file, which would also run
    # its boot logic) -- each line is a plain VAR="value" bash assignment,
    # so eval-ing just that one line and reading it back is the correct way
    # to get exactly what the operator typed, quoting included.
    kv_current_value() {
        local var="$1" line
        line="$(grep -E "^${var}=" custom-data.sh | tail -n1)"
        ( eval "$line"; printf '%s' "${!var}" )
    }

    echo "== uploading secrets to '$KEYVAULT_NAME'"
    KV_VARS="SUPABASE_URL SUPABASE_KEY GEMINI_API_KEY SARATHI_TOKEN WEBHOOK_URL YANTRA_WEBHOOK_SECRET TWILIO_SID TWILIO_TOKEN TWILIO_FROM TWILIO_TO"
    for VAR in $KV_VARS; do
        case "$VAR" in
            SUPABASE_URL) SECRET_NAME="supabase-url" ;;
            SUPABASE_KEY) SECRET_NAME="supabase-key" ;;
            GEMINI_API_KEY) SECRET_NAME="gemini-api-key" ;;
            SARATHI_TOKEN) SECRET_NAME="sarathi-token" ;;
            WEBHOOK_URL) SECRET_NAME="webhook-url" ;;
            YANTRA_WEBHOOK_SECRET) SECRET_NAME="yantra-webhook-secret" ;;
            TWILIO_SID) SECRET_NAME="twilio-sid" ;;
            TWILIO_TOKEN) SECRET_NAME="twilio-token" ;;
            TWILIO_FROM) SECRET_NAME="twilio-from" ;;
            TWILIO_TO) SECRET_NAME="twilio-to" ;;
        esac
        VALUE="$(kv_current_value "$VAR")"
        if [ -z "$VALUE" ]; then
            echo "   $VAR is empty -- skipping (leave $SECRET_NAME unset in the vault)"
            continue
        fi
        echo "   $SECRET_NAME"
        kv_secret_set_with_retry "$SECRET_NAME" "$VALUE"
    done

    echo "== building custom-data with secrets blanked (fetched from Key Vault at boot instead)"
    TMP_CUSTOM_DATA="$(mktemp)"
    cp custom-data.sh "$TMP_CUSTOM_DATA"
    for VAR in $KV_VARS; do
        # Blank the whole line by variable name, anchored at line start --
        # do NOT sed-substitute the secret VALUE anywhere: these can contain
        # characters (/, &, ...) that are unsafe inside a sed replacement.
        sed -i "s/^${VAR}=.*/${VAR}=\"\"/" "$TMP_CUSTOM_DATA"
    done
    sed -i "s/^KEYVAULT_NAME=.*/KEYVAULT_NAME=\"${KEYVAULT_NAME}\"/" "$TMP_CUSTOM_DATA"
    CUSTOM_DATA_FILE="$TMP_CUSTOM_DATA"

    VM_CREATE_EXTRA_ARGS+=(--assign-identity)
fi

VM_LOCATION=""
for LOC in $LOCATIONS; do
    echo "== creating VM '$VM_NAME' ($SIZE, $IMAGE) in '$LOC' — this takes a few minutes"
    OUT="$(mktemp)"
    if az vm create \
        --resource-group "$RG" \
        --name "$VM_NAME" \
        --location "$LOC" \
        --image "$IMAGE" \
        --size "$SIZE" \
        --os-disk-size-gb "$DISK_SIZE_GB" \
        --admin-username "$ADMIN_USER" \
        --generate-ssh-keys \
        --public-ip-sku Standard \
        --custom-data "$CUSTOM_DATA_FILE" \
        ${VM_CREATE_EXTRA_ARGS[@]+"${VM_CREATE_EXTRA_ARGS[@]}"} \
        -o table > "$OUT" 2>&1
    then
        cat "$OUT"; rm -f "$OUT"
        VM_LOCATION="$LOC"
        if [ "$USE_KEYVAULT" = "true" ]; then
            # Grant the VM's own managed identity read access to the vault
            # right away -- RBAC propagation can lag ~1-2 minutes, and this
            # is exactly what custom-data.sh's boot-time fetch is racing
            # against, so the earlier this fires the better.
            echo "== granting the VM's managed identity 'Key Vault Secrets User' on '$KEYVAULT_NAME'"
            VM_PRINCIPAL_ID="$(az vm show --resource-group "$RG" --name "$VM_NAME" --query identity.principalId -o tsv)"
            IDENTITY_GRANT_OUT="$(az role assignment create \
                --assignee-object-id "$VM_PRINCIPAL_ID" \
                --assignee-principal-type ServicePrincipal \
                --role "Key Vault Secrets User" \
                --scope "$KV_ID" -o none 2>&1)" || {
                if printf '%s' "$IDENTITY_GRANT_OUT" | grep -qi 'RoleAssignmentExists\|already exists'; then
                    echo "   already granted (re-run) -- continuing"
                else
                    echo "$IDENTITY_GRANT_OUT" >&2
                    echo "ERROR: could not grant the VM's managed identity access to the Key Vault." >&2
                    exit 1
                fi
            }
        fi
        break
    fi
    if grep -q 'SkuNotAvailable\|Capacity Restrictions' "$OUT"; then
        echo "   '$SIZE' has no capacity in '$LOC' right now (not a quota or account" >&2
        echo "   problem — just this region being out of stock) — trying the next region." >&2
        rm -f "$OUT"
        continue
    fi
    if grep -q 'DisallowedLocation' "$OUT"; then
        echo "   this subscription can't deploy to '$LOC' (a subscription-level region" >&2
        echo "   restriction, not a mistake on your part) — trying the next region." >&2
        rm -f "$OUT"
        continue
    fi
    # Any other failure: show it in full and stop — don't mask a real
    # problem (bad image ref, RBAC, custom-data content, real quota
    # exhaustion, etc.) by silently churning through the rest of the list.
    cat "$OUT" >&2
    rm -f "$OUT"
    echo "ERROR: az vm create failed for a reason other than regional" >&2
    echo "capacity/permission (see output above) — not retrying further regions." >&2
    exit 1
done

if [ -z "$VM_LOCATION" ]; then
    echo "ERROR: '$SIZE' had no capacity in any of: $LOCATIONS" >&2
    echo "Try a different size, e.g. SIZE=Standard_B2ms ./deploy.sh, or add more" >&2
    echo "regions: LOCATIONS=\"$LOCATIONS eastus westeurope\" ./deploy.sh" >&2
    exit 1
fi
echo "== VM created in '$VM_LOCATION'"

echo "== opening port 80 (nginx / the console)"
az vm open-port --resource-group "$RG" --name "$VM_NAME" --port 80 --priority 900 -o table

IP="$(az vm show -d --resource-group "$RG" --name "$VM_NAME" --query publicIps -o tsv)"

cat <<EOF

== VM created. Cloud-init is now installing YantraFleet on first boot —
   give it 3-5 minutes, then open:

     http://$IP/

   SSH in to watch progress or debug:

     ssh $ADMIN_USER@$IP
     tail -f /var/log/yantrafleet-install.log

   Tear everything down (stops all billing for this deployment):

     az group delete --name $RG --yes --no-wait
EOF
