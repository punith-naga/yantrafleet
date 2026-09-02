# Deploying YantraFleet on AWS (console only — no CLI, no Terraform)

This guide takes you from "repo on my laptop" to "fleet console on
`http://<public-ip>/`" using nothing but the AWS web console and one
copy-pasted script. Budget ~20 minutes, of which ~5 are waiting for the
instance to install itself.

What you end up with, on a single small EC2 instance:

| Piece            | Where it runs                              |
|------------------|--------------------------------------------|
| Console (UI)     | nginx serving `/opt/yantrafleet/console` at `http://<public-ip>/` |
| Academy (training) | nginx serving `/opt/yantrafleet/academy` at `http://<public-ip>/academy/` |
| Docs site        | nginx serving `/opt/yantrafleet/docs` at `http://<public-ip>/docs/` |
| Copilot API      | `yantra-sarathi` (uvicorn on 127.0.0.1:8001, proxied at `/ask` + `/health`) |
| Incident detector| `yantra-detect` systemd service            |
| Notifier         | `yantra-notify` systemd service            |
| Demo simulator   | `yantra-sim` — installed, **disabled** (enable only for demo fleets) |

The data itself lives in your Supabase project; the instance only reads
and writes it, so the box is disposable.

---

## Step 0 — Push the repo to a private GitHub repository

The instance clones your repo over HTTPS at boot, so it needs to be on
GitHub (private is fine) first.

If you received YantraFleet as a **git bundle** (a single `.bundle`
file), turn it into a normal checkout first:

```bash
git clone yantrafleet.bundle yantrafleet
cd yantrafleet
```

Then create an empty **private** repo in the GitHub web UI
(github.com -> "+" -> New repository -> name it `yantrafleet`, Private,
no README) and push:

```bash
cd yantrafleet
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/<YOUR_USERNAME>/yantrafleet.git
git push -u origin main        # or 'master' — whatever `git branch` says
```

For a **private** repo the instance also needs a token to clone:
GitHub -> Settings -> Developer settings -> Personal access tokens ->
Fine-grained tokens -> Generate. Scope it to only this repository with
**Contents: Read-only**, copy the token, and use it inside the clone URL
in the next step:

```
REPO_URL="https://<YOUR_TOKEN>@github.com/<YOUR_USERNAME>/yantrafleet.git"
```

## Step 1 — Prepare your user-data script

Open [`user-data.sh`](user-data.sh) in a text editor and fill in the
`=== EDIT ME ===` block at the top:

* `REPO_URL` — your GitHub HTTPS URL (with the token for private repos).
* `SUPABASE_URL` / `SUPABASE_KEY` — Supabase dashboard -> Settings ->
  API. Use the **publishable (anon)** key; it ends up in the browser URL
  by design. Never put the `service_role` key here.
* `YANTRA_SITE_ID` — e.g. `BLR-DC1`.
* Optionally `GEMINI_API_KEY` (copilot LLM answers) and `SARATHI_TOKEN`
  (bearer auth on `/ask`).

Keep the edited file handy — you paste the whole thing in step 2.

> Supabase schema: if you haven't yet, apply the migrations
> (`supabase/0001…0006`) to your project first — easiest from any
> machine with `python -m yantraops migrate --db-url "postgresql://..."`
> (see `supabase/README.md`), or paste each `.sql` file into the
> Supabase dashboard's SQL editor in filename order.

## Step 2 — Launch the instance (AWS console)

1. Sign in to the AWS console, pick your region (top right), and open
   **EC2 -> Instances -> Launch instances**.
2. **Name**: `yantrafleet`.
3. **Application and OS Images**: *Ubuntu*, and pick
   **Ubuntu Server 24.04 LTS (HVM), SSD Volume Type** (64-bit x86).
4. **Instance type**: `t3.small` (2 vCPU / 2 GiB — enough for all
   services; `t3.micro` is too tight once the copilot is loaded).
5. **Key pair**: create or select one (you'll want SSH for step 4).
6. **Network settings** -> Edit -> Security group rules:
   * Rule 1: **SSH**, port 22, Source = **My IP**.
   * Rule 2: **HTTP**, port 80, Source = **0.0.0.0/0** (Anywhere-IPv4).
   * Nothing else — the copilot port 8001 stays loopback-only on the box.
7. **Configure storage**: `20` GiB, **gp3**.
8. **Advanced details** -> scroll to the bottom -> **User data**: paste
   the entire contents of your edited `user-data.sh`.
9. **Launch instance**.

## Step 3 — Wait ~5 minutes, then open the console

Find the instance's **Public IPv4 address** on its EC2 detail page and
open:

```
http://<PUBLIC_IP>/
```

nginx 302-redirects `/` to
`/index.html?supa=<your-supabase>&key=<anon-key>&site=<site-id>`, so the
console is already pointed at your backend. The operator academy lives at
`http://<PUBLIC_IP>/academy/` (same redirect trick, same backend params)
and the docs site at `http://<PUBLIC_IP>/docs/`. If the page doesn't load
yet, the install is probably still running — give it another minute or
two (a fresh apt + pip install takes a while on a t3.small).

No robots on screen? That's expected until something writes rows: either
real robots via the connector, or the demo simulator
(`sudo systemctl enable --now yantra-sim`, see step 4).

## Step 4 — Verify from SSH

EC2 -> select the instance -> **Connect** (browser-based EC2 Instance
Connect works, or your own SSH client with the key pair):

```bash
# install log (cloud-init ran user-data once at first boot)
tail -50 /var/log/yantrafleet-install.log

# all services green?
systemctl status 'yantra-*'

# follow the copilot API live
journalctl -u yantra-sarathi -f

# copilot answering through nginx?
curl -s http://127.0.0.1/health
```

`systemctl status` should show `yantra-detect`, `yantra-notify` and
`yantra-sarathi` as `active (running)` (and `yantra-sim` as inactive
unless you enabled it).

## Updating to a new version

```bash
sudo -u yantra git -C /opt/yantrafleet pull
sudo -u yantra INSTALL_VENV_DIR=/opt/yantrafleet/.venv bash /opt/yantrafleet/install.sh
sudo systemctl restart yantra-detect yantra-notify yantra-sarathi
# add yantra-sim to that list only if you enabled the demo simulator
sudo nginx -t && sudo systemctl reload nginx
```

(If `deploy/aws/systemd/*.service` or the nginx template changed in the
pull, re-run the relevant install steps:
`sudo install -m 644 /opt/yantrafleet/deploy/aws/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload`.)

## What it costs

Roughly **USD 15–18/month** in most regions:

* t3.small on-demand: ~USD 12–15/mo depending on region
* 20 GiB gp3: ~USD 1.60/mo
* Public IPv4 address: ~USD 3.60/mo (AWS charges for IPv4 since 2024)
* Data transfer: negligible at demo traffic levels

Stop the instance when idle and you pay only for the disk and the IP.

## Hardening checklist (before showing this to the internet for real)

- [ ] **HTTPS**: point a DNS name at the IP, then
      `sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx`
      — certbot rewrites the nginx site for TLS and auto-renews.
- [ ] **Restrict port 80** (or 443 after certbot) in the security group
      to your office/VPN CIDR instead of 0.0.0.0/0 if the console is
      internal-only.
- [ ] **Set `SARATHI_TOKEN`** in `/etc/yantrafleet.env` (then
      `sudo systemctl restart yantra-sarathi`) so `/ask` requires a
      bearer token — otherwise anyone reaching the page can query the
      copilot.
- [ ] **Apply migration `0006_harden.sql`** (RLS lockdown) to the
      Supabase project so the anon key in the page URL can only do what
      the console needs.
- [ ] **Rotate keys** on a schedule: the Supabase anon key (dashboard ->
      Settings -> API -> roll), the GitHub token used in `REPO_URL`
      (delete it after first boot if you don't plan to `git pull` — or
      swap the remote to a fresh read-only token), and `GEMINI_API_KEY`.
      After rotating, update `/etc/yantrafleet.env` +
      `/etc/nginx/sites-available/yantrafleet` (the key appears in the
      302 redirect) and restart services / reload nginx.
- [ ] `sudo apt install unattended-upgrades` for automatic security
      patches.

## Files in this kit

```
deploy/aws/
├── README.md                        # this guide
├── user-data.sh                     # paste into EC2 "User data" (after editing)
├── validate.sh                      # local sanity checks for the kit
├── systemd/
│   ├── yantra-detect.service
│   ├── yantra-notify.service
│   ├── yantra-sarathi.service
│   └── yantra-sim.service           # installed, disabled by default
└── nginx/
    └── yantrafleet.conf.template    # envsubst'd to /etc/nginx/sites-available/yantrafleet
```
