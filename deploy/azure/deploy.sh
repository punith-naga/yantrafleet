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
#   RG=myrg LOCATION=eastus SIZE=Standard_B2s ./deploy.sh   # override any var

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

RG="${RG:-yantrafleet-rg}"
LOCATION="${LOCATION:-centralindia}"
VM_NAME="${VM_NAME:-yantrafleet}"
ADMIN_USER="${ADMIN_USER:-yantra}"
IMAGE="${IMAGE:-Canonical:ubuntu-24_04-lts:server:latest}"
SIZE="${SIZE:-Standard_B2s}"       # 2 vCPU / 4 GiB — comfortably runs all services
DISK_SIZE_GB="${DISK_SIZE_GB:-30}"

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

if grep -q 'YOUR_GITHUB_USERNAME\|YOUR_PROJECT_REF\|YOUR_SUPABASE_PUBLISHABLE' custom-data.sh; then
    echo "ERROR: custom-data.sh still has placeholders in its EDIT ME block." >&2
    echo "Open deploy/azure/custom-data.sh and fill in REPO_URL / SUPABASE_URL /" >&2
    echo "SUPABASE_KEY (and optionally the Gemini/Sarathi/notifier vars) first." >&2
    exit 1
fi

echo "== subscription in use:"
az account show --query '{name:name, id:id}' -o table

echo "== creating resource group '$RG' in '$LOCATION' (no-op if it exists)"
az group create --name "$RG" --location "$LOCATION" -o table

echo "== creating VM '$VM_NAME' ($SIZE, $IMAGE) — this takes a few minutes"
az vm create \
    --resource-group "$RG" \
    --name "$VM_NAME" \
    --image "$IMAGE" \
    --size "$SIZE" \
    --os-disk-size-gb "$DISK_SIZE_GB" \
    --admin-username "$ADMIN_USER" \
    --generate-ssh-keys \
    --public-ip-sku Standard \
    --custom-data custom-data.sh \
    -o table

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
