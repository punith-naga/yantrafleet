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
  fine-grained, read-only token pattern from the AWS guide applies here).
  Unlike AWS, you don't need to worry about revoking it afterward: with
  `USE_KEYVAULT=true` (the default), `deploy.sh` uploads this whole value
  to Key Vault and the VM never sees it in its stored `custom_data` — see
  "Secrets in Key Vault, not in custom_data, by default" below.
* `SUPABASE_URL` / `SUPABASE_KEY` — your Supabase project's publishable
  key.
* `YANTRA_SITE_ID` — e.g. `BLR-DC1`.
* Optionally `DOMAIN_NAME` + `CERTBOT_EMAIL` — set both to get HTTPS
  automatically at first boot (point the domain's DNS at the VM's public
  IP before it boots; `deploy.sh` already opens port 443 for you). These
  aren't secrets, so they stay in this file even with Key Vault on.
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
ports 80 and 443, and prints the public IP. Override any default inline,
e.g.:

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

## Secrets in Key Vault, not in custom_data, by default

Every value in the "EDIT ME" block of `custom-data.sh` used to go
straight into the VM's `custom_data` property, because `deploy.sh` passed
the whole file to `az vm create --custom-data` unmodified. `custom_data`
is not a boot-time-only scratch area -- it's stored on the VM resource
itself, in Azure's control plane, forever. Anyone with Reader on the VM
(Portal, `az vm show`, an ARM export, a misconfigured RBAC grant down the
road) could read `SUPABASE_KEY`, `GEMINI_API_KEY`, `TWILIO_TOKEN`, and the
rest back out in plaintext at any point after deploy, not just during the
few seconds cloud-init needs them at first boot. That's a much bigger
blast radius than "leaked once during provisioning" -- it's "leaked for
as long as the VM resource exists."

`deploy.sh` fixes that for the credential-shaped values by default:
`REPO_URL`, `SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY`,
`SARATHI_TOKEN`, `WEBHOOK_URL`, `YANTRA_WEBHOOK_SECRET`, `TWILIO_SID`,
`TWILIO_TOKEN`, `TWILIO_FROM`, and `TWILIO_TO` go into an Azure Key Vault
instead, and the VM fetches them at boot using its own managed identity
(no credential of any kind embedded anywhere for this). `REPO_URL` is
included precisely because it's credential-shaped too -- a private repo's
URL embeds a GitHub PAT (`https://<token>@github.com/...`), which sat in
`custom_data` in plaintext just like the other ten before this fix.
`DOMAIN_NAME`, `CERTBOT_EMAIL`, and `YANTRA_SITE_ID` are the only EDIT ME
values left out of Key Vault -- none of the three are secrets.

You still fill in the same "EDIT ME" block the same way in Step 2 --
nothing changes about how you author `custom-data.sh`. What changed is
what `deploy.sh` does with those values afterward: it creates (or reuses)
a Key Vault named after your VM, uploads the 11 credential values there,
blanks those same 11 lines out of the copy of `custom-data.sh` it actually
hands to Azure, and leaves cloud-init to pull the real values back from
the vault during boot instead of finding them sitting in `custom_data`.
By the time `az vm create` returns, the VM resource's `custom_data`
never had the credential values in it at all -- including `REPO_URL`, so
(unlike the AWS kit) there's no "revoke your PAT after first boot" step
here: it was never in the VM's stored `custom_data` to begin with. It
does still end up in `/opt/yantrafleet/.git/config`'s remote URL on the
box itself once cloned, same as any git checkout of a private repo with
an embedded-credential URL -- that requires root shell access on the VM
to read, a much higher bar than "anyone with Reader on the VM resource."

This is automatic. You do not need to create a Key Vault, assign a role,
or do anything differently -- `deploy.sh` handles vault creation, granting
itself "Key Vault Secrets Officer" so it's allowed to upload secrets into
an RBAC-mode vault, granting the VM's managed identity "Key Vault Secrets
User" so it can read them back at boot, and uploading the 11 values, all
as part of the normal `./deploy.sh` run from Step 3. It costs nothing
extra to notice -- the script just takes a little longer than before
(mostly RBAC propagation, which can lag a minute or two even for the
identity that just created the role assignment) and prints its own
"== doing thing" progress lines for each stage like the rest of the
script does.

### Opting out: `USE_KEYVAULT=false`

```bash
USE_KEYVAULT=false ./deploy.sh
```

With this set, behavior is exactly what it was before this change:
`custom-data.sh` is passed to `az vm create --custom-data` unmodified, no
Key Vault is created, no role assignments happen, and the 11 credential
values (including `REPO_URL`) sit in `custom_data` in plaintext same as
always -- in this mode, treat `REPO_URL`'s PAT the same way the AWS guide
does: fine-grained, read-only, single-repo, and revoked once you're done
with first boot if you don't plan to `git pull` again soon. Reach for
`USE_KEYVAULT=false` when:

* your subscription doesn't have permission to create Key Vaults or role
  assignments (a locked-down corporate or trial subscription, for
  example) and you'd rather deploy with the old behavior than have
  `deploy.sh` fail partway through trying to set up RBAC it can't have;
* you're doing a quick throwaway test deploy and don't want the extra
  minute or two of Key Vault setup / RBAC propagation time;
* you're debugging the deploy flow itself and want one fewer moving part.

Everything else about the deploy is identical either way -- same VM, same
services, same console URL. `USE_KEYVAULT=false` only changes where the
11 credential values are stored.

### Rotating a secret later

Because the values live in Key Vault, rotating one doesn't mean tearing
down and recreating the VM. From your own machine (wherever you run
`az`, logged into the same subscription):

```bash
az keyvault secret set --vault-name <your-vault-name> \
  --name <secret-name> --value "<new-value>"
```

(`<your-vault-name>` is whatever `deploy.sh` printed when it created the
vault -- derived from `VM_NAME`, e.g. `yantrafleet-kv` for the default
`VM_NAME`; `<secret-name>`
is the hyphenated form, e.g. `twilio-token` for `TWILIO_TOKEN` --
Key Vault doesn't allow underscores in secret names.)

That updates the secret in Key Vault, but **it does not, by itself,
change anything on the running VM.** cloud-init (and therefore
`custom-data.sh`'s Key Vault fetch) only runs once, at first boot. A
plain `sudo reboot` will not re-run it and will not pick up the new
value -- the service will happily keep using whatever it already loaded
into `/etc/yantrafleet.env` at first boot. To actually apply a rotated
secret, SSH into the VM and run the refresh script installed for exactly
this:

```bash
ssh yantra@<PUBLIC_IP>
sudo /opt/yantrafleet/refresh-secrets.sh
```

That re-fetches the 10 values the running services actually read from Key
Vault via the same IMDS + managed identity path cloud-init used at first
boot, rewrites `/etc/yantrafleet.env` (leaving `YANTRA_SITE_ID` untouched
-- it isn't part of the Key Vault flow), and restarts `yantra-detect`,
`yantra-notify`, and `yantra-sarathi` so the new values take effect
immediately. If you deployed with `USE_KEYVAULT=false`, this script has
nothing to refresh from and will refuse to run with a clear error --
rotate by editing `custom-data.sh` and recreating the VM in that case,
same as before this change existed.

`REPO_URL` (Key Vault secret `repo-url`) is the eleventh value and is
**not** part of this refresh -- none of the three services above read it,
only `git clone`/`git pull` do. Rotating it in Key Vault only matters the
next time you update the box:

```bash
az keyvault secret set --vault-name <your-vault-name> \
  --name repo-url --value "https://<new-token>@github.com/<you>/yantrafleet.git"
ssh yantra@<PUBLIC_IP>
sudo -u yantra git -C /opt/yantrafleet pull "https://<new-token>@github.com/<you>/yantrafleet.git" main
```

## Turning on every functionality

Same as the AWS kit's Step 5 — demo simulator (`sudo systemctl enable
--now yantra-sim`), confirming notifier channels, RBAC bootstrap (apply
`0006_harden.sql` + `0007_rbac.sql`, seed the first admin with
`yantraops grant-role` before relying on them), and `audit-security` from
the box itself. See `deploy/aws/README.md`'s "Step 5 — turn on every
functionality" — it's provider-agnostic, nothing there is AWS-specific.

## "SkuNotAvailable" / region capacity, and quota

Two different things can stop `az vm create`, and `deploy.sh` now handles
the first one for you automatically:

**Regional capacity ("SkuNotAvailable")** and **subscription region
restrictions ("DisallowedLocation")** — both hit during this kit's own
live testing. `Standard_B2s` had *"failed for Capacity Restrictions"* in
`centralindia` at one point (the datacenter was simply out of that size,
nothing to do with your account or quota), and separately the
fallback list's `jioindiawest` turned out not to be a region this
subscription is permitted to deploy to at all (a subscription-type
restriction — Azure returned the exact list of ~90 permitted regions for
this account in the error, which is how the default list below was
corrected). Neither is a mistake on your part. As of this commit,
`deploy.sh` retries past both automatically — it tries a short list of
regions in order (`centralindia indiasouthcentral westindia southeastasia
eastus2 uksouth centralus` by default) and stops at the first one that's
both permitted and has capacity, so you shouldn't need to do anything —
it just takes a little longer if the first region or two don't work out.
Override the list with `LOCATIONS="..."` or pin one region with
`LOCATION=...` (disables fallback). Note: Azure CLI's own error output
for either case can crash with a `UnicodeEncodeError` /
`RuntimeError: content already consumed` traceback instead of showing
the real message cleanly — that's a [known Azure CLI bug](https://github.com/azure/azure-cli/issues/32417),
not something wrong on your end; `deploy.sh` greps the raw output for
`SkuNotAvailable`/`DisallowedLocation` so it isn't fooled by that crash
either way.

**Subscription quota (0 vCPU for a family)** — different from the above:
a brand-new subscription can start with a **regional vCPU quota of 0**
for some VM families, which fails with a quota error rather than
capacity restriction. If every region in the fallback list fails with a
*quota* message (not `SkuNotAvailable`) instead: Azure Portal → search
"Quotas" → Compute → find the family (e.g. "Standard BSv2 Family
vCPUs") → Request increase (a small bump, e.g. to 4, is typically
auto-approved in minutes, not days).

## "UnicodeEncodeError: 'latin-1' codec can't encode characters..."

Hit this right after "creating VM ..." on Windows (Git Bash/MINGW64)? It's
an Azure CLI quirk, not your setup: `az vm create --custom-data
custom-data.sh` reads that file whole and base64-encodes it into the ARM
request body, and on some Windows Python installs the HTTP layer defaults
to a `latin-1` body encoding instead of UTF-8. Any non-ASCII character in
`custom-data.sh` (an em dash, a curly quote) then crashes the call with
this exact error, part-way through — after the resource group and VM
resource are already being created, so it can look scarier than it is.
Already fixed as of this commit (the file is now plain ASCII, and
`validate.sh` pins it so it can't quietly regress if it's edited again).
If you still hit it after pulling latest — e.g. you added your own
non-ASCII text to the EDIT ME block's comments — run `bash
deploy/azure/validate.sh` to find the offending line, or just re-run
`./deploy.sh`; `az vm create` fails loudly without creating a duplicate
VM, so retrying is safe.

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

Same list as the AWS kit: HTTPS (already done at boot if you set
`DOMAIN_NAME`/`CERTBOT_EMAIL` in Step 2, otherwise run certbot by hand the
same way AWS's guide describes), restrict inbound rules to your IP if
it's not meant to be public (`deploy.sh` opened 80 and 443 to everyone —
narrow with `az network nsg rule update` if needed), set `SARATHI_TOKEN`,
apply `0006_harden.sql`, rotate keys on a schedule (see "Rotating a
secret later" above for `REPO_URL` specifically).

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
