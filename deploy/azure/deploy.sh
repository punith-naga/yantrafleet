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
        --custom-data custom-data.sh \
        -o table > "$OUT" 2>&1
    then
        cat "$OUT"; rm -f "$OUT"
        VM_LOCATION="$LOC"
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
