# Deploying YantraFleet on Azure

Unlike the AWS kit (console-click, on purpose, because that's how that
guide was designed), this one is a single script you run yourself after
one interactive login. That's deliberate: driving your Azure Portal
session on your behalf isn't something Claude can safely do (no way to
share your logged-in browser session, and typing your Azure password in
anywhere is off the table), and clicking through Azure's own multi-page
VM wizard is more steps to describe than just handing you one script. The
`az login` step below opens *your own* browser, you approve it there, and
nothing about your Azure account ever passes through this chat.

What you end up with — identical to the AWS kit, same VM, same services:

| Piece            | Where it runs                              |
|------------------|--------------------------------------------|
| Console (UI)     | nginx serving `/opt/yantrafleet/console` at `http://<public-ip>/` |
| Academy (training) | nginx serving `/opt/yantrafleet/academy` at `http://<public-ip>/academy/` |
| Docs site        | nginx serving `/opt/yantrafleet/docs` at `http://<public-ip>/docs/` |
| Copilot API      | `yantra-sarathi` (uvicorn on 127.0.0.1:8001, proxied at `/ask` + `/health`) |
| Incident detector| `yantra-detect` systemd service            |
| Notifier         | `yantra-notify` systemd service            |
| Demo simulator   | `yantra-sim` — installed, **disabled** (enable only for demo fleets) |

The systemd units and nginx config are the exact same files the AWS kit
uses (`deploy/aws/systemd/`, `deploy/aws/nginx/`) — only the VM
provisioning step differs between clouds, so those aren't duplicated.

## Step 0 — install the Azure CLI, once

```powershell
winget install -e --id Microsoft.AzureCLI
```
(or see [learn.microsoft.com/cli/azure/install-azure-cli](https://learn.microsoft.com/cli/azure/install-azure-cli) for other OSes). Close and reopen your terminal afterward so `az` is on PATH.

## Step 1 — log in, once

```powershell
az login
```

This opens your default browser to a Microsoft sign-in page. Approve it
there. If you have more than one subscription, check you're on the right
one:

```powershell
az account show --query name -o tsv
# or: az account set --subscription "<name-or-id>"
```

## Step 2 — fill in the EDIT ME block

Open `deploy/azure/custom-data.sh` and fill in the same fields as the AWS
kit:

* `REPO_URL` — your GitHub repo (`punith-naga/yantrafleet` — use a
  `https://<token>@github.com/...` URL if it's private; the same
  fine-grained, read-only, short-lived token pattern from the AWS guide
  applies here).
* `SUPABASE_URL` / `SUPABASE_KEY` — your Supabase project's publishable
  key.
* `YANTRA_SITE_ID` — e.g. `BLR-DC1`.
* Optionally `GEMINI_API_KEY`, `SARATHI_TOKEN`, and the notifier block
  (`WEBHOOK_URL` / `TWILIO_*`) — leave blank to skip.

## Step 3 — run the deploy script

```powershell
cd deploy/azure
./deploy.sh
```

(From Git Bash / WSL — the script is bash. From plain PowerShell, run it
via `bash ./deploy.sh` if `./deploy.sh` isn't recognized.)

This creates one resource group (`yantrafleet-rg`), one Ubuntu 24.04 VM
(`Standard_B2s` — 2 vCPU / 4 GiB, in `centralindia` by default), opens
port 80, and prints the public IP. Override any default inline, e.g.:

```bash
LOCATION=westindia SIZE=Standard_B2s ./deploy.sh
```

## Step 4 — wait ~5 minutes, then open the console

The script prints the public IP directly. Same redirect trick as AWS:
`http://<PUBLIC_IP>/` 302s to the console with your Supabase params
already attached; `/academy/` and `/docs/` work the same way.

```bash
ssh yantra@<PUBLIC_IP>
tail -f /var/log/yantrafleet-install.log      # watch the install
systemctl status 'yantra-*'                    # confirm services are up
```

## Turning on every functionality

Same as the AWS kit's Step 5 — demo simulator (`sudo systemctl enable
--now yantra-sim`), confirming notifier channels, RBAC bootstrap (apply
`0006_harden.sql` + `0007_rbac.sql`, seed the first admin via SQL before
relying on them), and `audit-security` from the box itself. See
`deploy/aws/README.md`'s "Step 5 — turn on every functionality" — it's
provider-agnostic, nothing there is AWS-specific.

## New-subscription quota note

A brand-new Azure subscription (free trial or fresh pay-as-you-go) can
start with a **regional vCPU quota of 0** for some VM families, which
makes `az vm create` fail with a quota error rather than a vague hold —
Azure is more transparent about this than AWS's opaque "account
verification" message. If you hit it: Azure Portal → search "Quotas" →
Compute → find the family (e.g. "Standard BSv2 Family vCPUs" for
`Standard_B2s`) in your target region → Request increase (a small bump,
e.g. to 4, is typically auto-approved in minutes, not days). Switching
`LOCATION` to a different region sometimes has quota available
immediately with no request needed.

## What it costs

Roughly **USD 15–20/month** for `Standard_B2s` plus a Standard public IP
and a 30 GiB managed disk — check the [Azure Pricing
Calculator](https://azure.microsoft.com/pricing/calculator/) for your
exact region, since pricing varies by region and changes over time.
Delete the resource group when you're done experimenting and every
resource in it (VM, disk, IP, NIC) is gone and billing stops:

```bash
az group delete --name yantrafleet-rg --yes --no-wait
```

## Hardening checklist

Same list as the AWS kit: HTTPS via certbot, restrict inbound rules to
your IP if it's not meant to be public (`az vm open-port` above opened
80 to everyone — narrow it with `az network nsg rule update` if needed),
set `SARATHI_TOKEN`, apply `0006_harden.sql`, rotate keys on a schedule.

## Files in this kit

```
deploy/azure/
├── README.md          # this guide
├── custom-data.sh      # cloud-init script (fill in EDIT ME, then deploy.sh uses it)
├── deploy.sh            # the one command: az group create + az vm create + open port
└── validate.sh           # local sanity checks for this kit
```

(No `systemd/` or `nginx/` here on purpose — this kit reuses
`deploy/aws/systemd/` and `deploy/aws/nginx/` directly, since those files
have nothing AWS-specific in them.)

## Using multiple clouds to stretch free credit

Since the goal is running this for close to nothing: AWS, Azure, GCP, and
Oracle Cloud each give new accounts their own free-tier credit or
always-free compute, and nothing about this platform ties it to one
provider — `yantrabridge`/`yantrasim` just need *a* Linux box that can
reach your Supabase project. A natural way to stretch runway is running
the platform on whichever cloud currently has unused credit, or even
splitting components (e.g. the always-on services on one cloud's
always-free tier, a demo box spun up temporarily elsewhere). If you want
a similar CLI-based kit for GCP (`gcloud compute instances create`, free
`e2-micro` in specific US regions under the Always Free tier) or Oracle
Cloud (Always Free Ampere A1 VMs — genuinely free indefinitely, not just
a trial), say the word and I'll build it the same way this one was built:
reusing the existing systemd/nginx assets, one script, one login.
