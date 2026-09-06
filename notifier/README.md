# yantranotify — omnichannel alert notifier

Polls the Yantrika backend (PostgREST/Supabase) for **unacked
`crit`/`serious` alerts** and **newly `Open` incidents**, and pushes
human-readable notifications through pluggable channels:

| Channel  | Transport                                     | Enabled when |
|----------|-----------------------------------------------|--------------|
| console  | log lines                                     | always |
| webhook  | JSON POST — `json` \| `slack` (Block Kit) \| `discord` (embeds) | `WEBHOOK_URL` set (else prints payload) |
| whatsapp | Twilio Messages REST API (raw HTTP, no SDK)   | all `TWILIO_*` vars set (else prints payload) |

Alarm-fatigue discipline:

* **Dedup** — an alert/incident is announced at most once (in-memory,
  optionally persisted with `--state-file` so restarts stay quiet).
* **Digest** — more than 5 new events in one poll collapse into a single
  summary message instead of a message storm, grouped by severity with
  counts and the top 3 items per group.

Delivery discipline:

* **Retry** — every outbound send gets 3 attempts with exponential
  backoff (1s, then 4s) and a 5s request timeout.
* **No loss** — an item that still fails stays queued in-process and is
  retried on the next poll.
* **Circuit breaker** — after 5 consecutive failures a channel is paused
  for 5 minutes (logged once); queued items are delivered when it
  recovers.

## Run

```bash
pip install -e .            # deps: httpx only

python -m yantranotify --interval 10              # poll loop
python -m yantranotify --once                     # single poll
python -m yantranotify --interval 10 --dry-run    # print, never send
python -m yantranotify --once --state-file ~/.yantranotify.json
```

`--url/--key` (or `SUPABASE_URL`/`SUPABASE_KEY`) override the embedded
Supabase project defaults.

## Webhook payload formats

Pick with `--format {json,slack,discord}` or `WEBHOOK_FORMAT`; the flag
wins over the env var, default is `json`.

| Format    | Shape | Use with |
|-----------|-------|----------|
| `json`    | `{"text": "..."}` (generic contract, default) | Slack, Discord `/slack` endpoint, Teams adapters, anything |
| `slack`   | Block Kit: header with severity emoji, robot/site/time fields, context line; incidents get a distinct layout with an **Impact** field. Action-less — no buttons. | Slack incoming webhooks |
| `discord` | Embeds, colour-coded by severity (crit red, serious orange) | Discord webhooks (native URL, no `/slack` suffix) |

Payload templates are pure functions in `yantranotify/formats.py`:
`payload_for(event, fmt)` / `digest_payload_for(events, fmt)` — build a
dict, no I/O, trivially unit-testable.

## Slack webhook setup (walkthrough)

1. Go to <https://api.slack.com/apps> → **Create New App** → *From
   scratch*, name it (e.g. `Yantrika`) and pick your workspace.
2. In the app's sidebar open **Incoming Webhooks** and toggle
   **Activate Incoming Webhooks** on.
3. Click **Add New Webhook to Workspace**, choose the channel (e.g.
   `#fleet-alerts`) and **Allow**.
4. Copy the webhook URL (`https://hooks.slack.com/services/T…/B…/…`).
5. Verify it in ten seconds:

   ```bash
   export WEBHOOK_URL="https://hooks.slack.com/services/T000/B000/XXXX"
   python -m yantranotify test --format slack
   ```

   Two messages (a sample alert + a sample incident) appear in the
   channel; the rendered payloads are also printed to stdout.
6. Run for real:

   ```bash
   export WEBHOOK_FORMAT=slack
   python -m yantranotify --interval 10
   ```

## Discord webhook setup

1. Discord: channel **Settings → Integrations → Webhooks → New
   Webhook**, copy the URL (`https://discord.com/api/webhooks/…`).
2. Either use it as-is with rich embeds:

   ```bash
   export WEBHOOK_URL="https://discord.com/api/webhooks/…"
   export WEBHOOK_FORMAT=discord
   python -m yantranotify test        # verify, then drop `test`
   ```

   or append `/slack` to the URL and keep the default `json` format
   (Discord then accepts Slack-style `{"text": ...}`).

## Generic JSON contract (`--format json`, default)

One `POST` per notification, `Content-Type: application/json`:

```json
{"text": "[CRIT] ALERT A-017: battery critical (7%) · src=AMR-03 · at 14:07"}
```

Digests (more than 5 new events in one poll) are a single POST whose
`text` groups events by severity with counts and the top 3 per group.
Any 2xx response counts as delivered; anything else triggers the
retry/queue/breaker machinery above.

## Verifying delivery: `python -m yantranotify test`

```bash
python -m yantranotify test                          # print payloads only
python -m yantranotify test --format slack           # Block Kit preview
python -m yantranotify test --channel webhook \
    --url https://hooks.slack.com/services/T…/B…/… --format slack
```

Sends one sample alert and one sample incident through the *real*
delivery path (retries included) and prints both rendered payloads.
Without a URL (flag or `WEBHOOK_URL`) it prints and sends nothing.
Exit code is non-zero when delivery fails.

## WhatsApp via the Twilio sandbox

1. Create a free Twilio account and open **Messaging → Try it out →
   Send a WhatsApp message**. The sandbox number is `+1 415 523 8886`.
2. From your phone, send the shown join code (e.g. `join brown-cat`) to
   that number on WhatsApp — this opts your number in.
3. Export the credentials (Account SID + Auth Token from the console):

   ```bash
   export TWILIO_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
   export TWILIO_TOKEN="your_auth_token"
   export TWILIO_FROM="+14155238886"     # sandbox number
   export TWILIO_TO="+91XXXXXXXXXX"      # your opted-in number
   ```

   The `whatsapp:` prefix is added automatically if you omit it.
4. `python -m yantranotify --interval 10`

Credentials are **never required**: with any of the four unset, the
channel prints the exact Twilio payload it *would* send (dry-run), which
is also what `--dry-run` forces for every channel.

## Tests (fully offline)

```bash
pip install -e .[dev]
python -m pytest tests -q
```

Everything runs through `httpx.MockTransport` or a local
`http.server` stub — dedup, digest batching, webhook payload shapes
(Block Kit, Discord embeds, generic JSON), retry/backoff and the
circuit breaker (fake clock, no real sleeping), the `test` subcommand
end-to-end, Twilio request shape (URL, basic auth, form fields) and the
credential-free dry-run path. No network, no creds.
