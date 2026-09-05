# Deploying YantraFleet on AWS (console only — no CLI, no Terraform)

This guide takes you from "repo on my laptop" to "fleet console on
`http://<public-ip>/console/`" using nothing but the AWS web console and
one copy-pasted script. Budget ~20 minutes, of which ~5 are waiting for
the instance to install itself.

What you end up with, on a single small EC2 instance:

| Piece            | Where it runs                              |
|------------------|--------------------------------------------|
| Marketing site (landing/about/etc) | nginx serving `/opt/yantrafleet/marketing` at `http://<public-ip>/` |
| Console (UI)     | nginx serving `/opt/yantrafleet/console` at `http://<public-ip>/console/` |
| Academy (training) | nginx serving `/opt/yantrafleet/academy` at `http://<public-ip>/academy/` |
| Docs site        | nginx serving `/opt/yantrafleet/docs` at `http://<public-ip>/docs/` |
| Copilot API      | `yantra-sarathi` (uvicorn on 127.0.0.1:8001, proxied at `/ask` + `/health`) |
| Incident detector| `yantra-detect` systemd service            |
| Notifier         | `yantra-notify` systemd service            |
| Demo simulator   | `yantra-sim` — installed, **disabled** (enable only for demo fleets) |
| Conformance page | `marketing/vda-5050-conformance-test.html` at `http://<public-ip>/vda-5050-conformance-test.html` (generated `yantra-conform` reports link here) |
| Demo sandbox door | `yantra-sandbox` (stdlib HTTP on 127.0.0.1:8088, proxied at `/api/demo/`) — installed, **disabled**; needs `supabase/0009` first |
| Demo sandbox reaper | `yantra-sandbox-reap.timer` + `.service` — installed, **disabled**; purges expired sandboxes every minute |

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
* Optionally `DOMAIN_NAME` + `CERTBOT_EMAIL` — set both to get HTTPS
  automatically at first boot (see step 2's security group rule and the
  DNS note right after it). Leave both blank to stay on plain HTTP for now
  and add it later via the hardening checklist.
* Optionally `GEMINI_API_KEY` (copilot LLM answers) and `SARATHI_TOKEN`
  (bearer auth on `/ask`).
* Optionally the notifier fan-out block — `WEBHOOK_URL` (Slack/Discord
  incoming webhook), `YANTRA_WEBHOOK_SECRET` (HMAC-signs those posts),
  `TWILIO_SID`/`TWILIO_TOKEN`/`TWILIO_FROM`/`TWILIO_TO` (WhatsApp
  alerts). Leave any of these blank to skip that channel — the notifier
  always still logs to the journal either way.

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
   * Rule 3: **HTTPS**, port 443, Source = **0.0.0.0/0** — needed if you
     filled in `DOMAIN_NAME`/`CERTBOT_EMAIL` in step 1, or plan to add
     HTTPS by hand later. Harmless to open even if you never use it: nginx
     has nothing listening on 443 until certbot runs.
   * Nothing else — the copilot port 8001 stays loopback-only on the box.
7. **Configure storage**: `20` GiB, **gp3**.
8. **Advanced details** -> scroll to the bottom -> **User data**: paste
   the entire contents of your edited `user-data.sh`.
9. **Launch instance**.

> Using `DOMAIN_NAME`? Point its DNS A/AAAA record at the Elastic
> IP/public IP you plan to use *before* launching (or launch first, grab
> the public IP, then create the DNS record and reboot the instance to
> re-run cloud-init — see "Updating to a new version" below for the
> re-run command). Let's Encrypt validates over the network at boot time,
> so if DNS isn't live yet, certbot just warns and leaves the site on
> plain HTTP instead of failing the whole install.

## Step 3 — Wait ~5 minutes, then open the console

Find the instance's **Public IPv4 address** on its EC2 detail page. The
bare host is the public marketing/landing site:

```
http://<PUBLIC_IP>/
```

The console lives at `http://<PUBLIC_IP>/console/` — nginx 302-redirects
bare `/console` to
`/console/index.html?supa=<your-supabase>&key=<anon-key>&site=<site-id>`,
so it's already pointed at your backend. The operator academy lives at
`http://<PUBLIC_IP>/academy/` (same redirect trick, same backend params)
and the docs site at `http://<PUBLIC_IP>/docs/`. If nothing loads yet,
the install is probably still running — give it another minute or two (a
fresh apt + pip install takes a while on a t3.small).

The full URL layout nginx serves (one server block, see
`deploy/aws/nginx/yantrafleet.conf.template`):

| URL | What answers | Anonymous? |
|---|---|---|
| `/` | marketing site, static, from `/opt/yantrafleet/marketing` | yes |
| `/vda-5050-conformance-test.html` | the conformance-tester landing page; every report `yantra-conform` generates links here, so the filename is pinned by both `validate.sh` scripts | yes |
| `/console/` | fleet console (302 from bare `/console` with `supa`/`key`/`site` attached) | yes |
| `/academy/` | operator academy (same redirect trick) | yes |
| `/docs/` | the checked-in docs site; `*.md` under it is served as `text/plain` so a browser renders it instead of downloading it | yes |
| `/ask` | Sarathi copilot API -> `127.0.0.1:8001`; set `SARATHI_TOKEN` to require a bearer token | yes, unless `SARATHI_TOKEN` is set |
| `/health` | copilot liveness -> `127.0.0.1:8001` | yes |
| `/api/demo/session` (POST) | mints one throwaway demo sandbox -> `127.0.0.1:8088` | yes — **and it writes**, see below |
| `/api/demo/limits` (GET) | whether the demo button should be shown | yes |
| *(not exposed)* `/healthz` | the sandbox door's own health, loopback only | no |

`/api/demo/` is the only anonymous route on the box that creates database
rows, so it carries an nginx rate limit (`limit_req_zone yantra_demo`,
6r/m with a burst of 3) on top of the application's own per-IP quota. Every
location in that template carries a comment saying why it is safe to expose;
if you add one, add that comment — `validate.sh` checks for it.

### Turning on the zero-signup live demo (optional)

The marketing site's primary call to action is a "Try it with a live fleet"
button that mints a throwaway sandbox with no signup. It is **off by
default**, because it grants anonymous visitors write access. To enable it:

1. **Read `supabase/0009_demo_sandbox.sql`**, in particular its THREAT MODEL
   block, then apply it:

   ```bash
   sudo -u yantra /opt/yantrafleet/.venv/bin/python -m yantraops migrate
   ```

2. **Give the reaper a service_role key.** `yantra-sandbox` itself runs on
   the *anon* key from `/etc/yantrafleet.env` and must keep it — that is what
   0009's row-level security confines. But `demo_reap_expired()` is granted
   to `authenticated` and `service_role` and **never** to `anon`, so the
   reaper cannot use that key. It reads a second env file after the first,
   and that file's `SUPABASE_KEY` wins for that unit only:

   ```bash
   printf 'SUPABASE_KEY=%s\n' '<your service_role key>' \
     | sudo tee /etc/yantrafleet-sandbox.env >/dev/null
   sudo chown root:yantra /etc/yantrafleet-sandbox.env
   sudo chmod 640 /etc/yantrafleet-sandbox.env
   ```

   Mode `640`, owner `root`, group `yantra` — the same as
   `/etc/yantrafleet.env`: readable by the service user and nobody else.
   Keeping the key in a file rather than on the command line is what keeps
   it out of `ps`.

3. **Enable both units** — the door and the timer that cleans up after it.
   Without the timer, expired sandbox rows and orphaned simulator processes
   accumulate forever:

   ```bash
   sudo systemctl enable --now yantra-sandbox yantra-sandbox-reap.timer
   systemctl list-timers yantra-sandbox-reap.timer
   curl -s localhost:8088/healthz
   curl -s localhost/api/demo/limits
   ```

Skip step 2 and the reaper runs every minute and fails with a permission
error every minute. Skip step 3's timer and nothing reaps at all.

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

**If you used a private repo, revoke that PAT now.** Unlike the Azure kit
(which pulls secrets from Key Vault at boot so nothing sensitive sits in
the instance's stored user-data), this AWS kit is console-only with no
vault to fetch from — so the token you embedded in `REPO_URL` is sitting
in this instance's user-data for as long as the instance exists, readable
by anyone with `ec2:DescribeInstanceAttribute` on your account, and it
also persists in `/opt/yantrafleet/.git/config`'s stored remote URL on the
box itself. Since you scoped it to this one repo, read-only, in step 0,
revoking it costs nothing right now — you only need a token again when
you next `git pull` (see "Updating to a new version" below, which creates
a fresh short-lived one for exactly that, instead of leaving a standing
one). GitHub -> Settings -> Developer settings -> Personal access tokens
-> Fine-grained tokens -> find it -> **Delete**.

## Step 5 — turn on every functionality

The box is running with the core services (detector, notifier, sarathi)
live from boot. A few features are opt-in — either because they need
data flowing (a fleet), or because they need a decision only you can
make (who's an admin, which channels to notify).

**See something on the map.** Either point `yantrabridge` at your real
robots' MQTT broker (not covered by this kit — see `connector/README.md`
for the connector's own deployment), or, for a demo/eval box, turn on the
bundled simulator:

```bash
sudo systemctl enable --now yantra-sim
```

**Confirm the notifier's extra channels are live** (if you filled in
`WEBHOOK_URL` / `TWILIO_*` in step 1 — they don't need a restart, they
were already in `/etc/yantrafleet.env` at first boot, but it's worth
checking):

```bash
sudo systemctl status yantra-notify
journalctl -u yantra-notify -n 30 --no-pager
```

A channel with no credentials set prints its payload to the journal
instead of sending — that's expected, not a failure.

**Turn on RBAC** (role-based access instead of "anyone with the link can
approve commands"). This needs migrations `0006_harden.sql` and
`0007_rbac.sql` applied to your Supabase project (both are OPT-IN —
`python -m yantraops migrate --db-url "..." --include-opt-in`, or paste
them into the SQL editor in order, *after* 0001–0005). Then seed your
first admin — do this **before** relying on 0006/0007, otherwise nobody
can grant roles through the API. Sign up that email via the console's
Create Account tab first (grant-role needs a matching `auth.users` row),
then from anywhere with the same `--db-url`:

```bash
python -m yantraops grant-role --db-url "..." \
    --email you@example.com --role admin        # match your YANTRA_SITE_ID
                                                  # with --site if not BLR-DC1
```

(Prefer the SQL editor? The equivalent statement is at the bottom of
`0007_rbac.sql`, commented out.)

From here you (as admin) can grant `operator`/`engineer`/`manager` roles
to other accounts the same way (`grant-role --role operator ...`); the
console's Pending Approvals card starts enforcing `decide_command`
(manager+) at that point.

**Verify the whole posture from the box itself** (it has real internet
access, unlike a locked-down laptop network):

```bash
sudo -u yantra /opt/yantrafleet/.venv/bin/python -m yantraops audit-security \
  --url "$(grep ^SUPABASE_URL /etc/yantrafleet.env | cut -d= -f2)" \
  --key "$(grep ^SUPABASE_KEY /etc/yantrafleet.env | cut -d= -f2)"
```

Expect `mode: demo` before step 5's RBAC migrations, `mode: rbac` after.

## Updating to a new version

If your repo is public, or you kept a token on the box, a plain pull works:

```bash
sudo -u yantra git -C /opt/yantrafleet pull
```

If you revoked the PAT after first boot (recommended — see step 4), the
stored `origin` remote has no credential anymore. Rather than putting a
long-lived token back, create a fresh **fine-grained, read-only,
single-repo** token, use it for one pull, then let it expire/delete it:

```bash
sudo -u yantra git -C /opt/yantrafleet pull \
    "https://<NEW_TOKEN>@github.com/<you>/yantrafleet.git" main
```

That pulls straight from the URL you pass without touching the stored
`origin` remote at all, so nothing new is left sitting in `.git/config`
once it's done. Either way, finish with:

```bash
sudo -u yantra INSTALL_VENV_DIR=/opt/yantrafleet/.venv bash /opt/yantrafleet/install.sh
sudo systemctl restart yantra-detect yantra-notify yantra-sarathi
# add yantra-sim to that list only if you enabled the demo simulator
sudo nginx -t && sudo systemctl reload nginx
```

**If the pull changed `deploy/aws/systemd/*.service`**, reinstall the units:

```bash
sudo install -m 644 /opt/yantrafleet/deploy/aws/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart yantra-detect yantra-notify yantra-sarathi
```

**If the pull changed `deploy/aws/nginx/yantrafleet.conf.template`** (it
did in v0.13: the marketing site moved to `/` and the console to
`/console/`), `git pull` alone does *not* pick that up — the live site
file at `/etc/nginx/sites-available/yantrafleet` is only rendered from
the template by the boot script. Re-render it by hand with the same
values you used at boot (they're in `/etc/yantrafleet.env`):

```bash
set -a; . /etc/yantrafleet.env; set +a
export NGINX_SERVER_NAME="${DOMAIN_NAME:-_}"     # or your real domain if you have one
envsubst '${SUPABASE_URL} ${SUPABASE_KEY} ${YANTRA_SITE_ID} ${NGINX_SERVER_NAME}' \
    < /opt/yantrafleet/deploy/aws/nginx/yantrafleet.conf.template \
    | sudo tee /etc/nginx/sites-available/yantrafleet >/dev/null
sudo nginx -t && sudo systemctl reload nginx
```

(If certbot already installed a certificate on this box, it edited the
site file in place — re-run `sudo certbot --nginx -d <domain> --redirect`
after the step above so its `listen 443` block is added back.)

## What it costs

Roughly **USD 15–18/month** in most regions:

* t3.small on-demand: ~USD 12–15/mo depending on region
* 20 GiB gp3: ~USD 1.60/mo
* Public IPv4 address: ~USD 3.60/mo (AWS charges for IPv4 since 2024)
* Data transfer: negligible at demo traffic levels

Stop the instance when idle and you pay only for the disk and the IP.

## Hardening checklist (before showing this to the internet for real)

- [ ] **HTTPS**: if you set `DOMAIN_NAME` + `CERTBOT_EMAIL` in step 1,
      this already happened at boot — check
      `grep certbot /var/log/yantrafleet-install.log` if you're not sure
      it succeeded. Otherwise, point a DNS name at the IP and run:
      `sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx --non-interactive --agree-tos -m you@example.com -d your-domain.example.com --redirect`
      — certbot rewrites the nginx site for TLS and auto-renews via its
      own `certbot.timer` (installed with the package, no cron needed).
- [ ] **Restrict port 80/443** in the security group to your office/VPN
      CIDR instead of 0.0.0.0/0 if the console is internal-only.
- [ ] **Set `SARATHI_TOKEN`** in `/etc/yantrafleet.env` (then
      `sudo systemctl restart yantra-sarathi`) so `/ask` requires a
      bearer token — otherwise anyone reaching the page can query the
      copilot.
- [ ] **Apply migration `0006_harden.sql`** (RLS lockdown) to the
      Supabase project so the anon key in the page URL can only do what
      the console needs.
- [ ] **Rotate keys** on a schedule: the Supabase anon key (dashboard ->
      Settings -> API -> roll), the GitHub token used in `REPO_URL` (see
      "revoke that PAT now" in step 4 and "Updating to a new version"
      above — there should be no standing token to rotate at all once
      you've done that), and `GEMINI_API_KEY`.
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
