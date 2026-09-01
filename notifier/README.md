# yantranotify — omnichannel alert notifier

Polls the YantraFleet backend (PostgREST/Supabase) for **unacked
`crit`/`serious` alerts** and **newly `Open` incidents**, and pushes
human-readable notifications through pluggable channels:

| Channel  | Transport                                     | Enabled when |
|----------|-----------------------------------------------|--------------|
| console  | log lines                                     | always |
| webhook  | `POST {"text": ...}` (Slack-compatible JSON)  | `WEBHOOK_URL` set (else prints payload) |
| whatsapp | Twilio Messages REST API (raw HTTP, no SDK)   | all `TWILIO_*` vars set (else prints payload) |

Alarm-fatigue discipline:

* **Dedup** — an alert/incident is announced at most once (in-memory,
  optionally persisted with `--state-file` so restarts stay quiet).
* **Digest** — more than 5 new events in one poll collapse into a single
  summary message instead of a message storm.

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

## Slack webhook setup

1. In Slack: **Apps → Incoming Webhooks → Add to Slack**, pick a channel,
   copy the URL (`https://hooks.slack.com/services/T…/B…/…`).
2. `export WEBHOOK_URL="https://hooks.slack.com/services/T000/B000/XXXX"`
3. `python -m yantranotify --interval 10`

The payload is plain `{"text": "..."}`, so the same variable also works
for Discord (append `/slack` to the Discord webhook URL) and for
Microsoft Teams via a workflow/adapter that accepts Slack-style text.

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

Everything runs through `httpx.MockTransport` — dedup, digest batching,
webhook payload shape, Twilio request shape (URL, basic auth, form
fields) and the credential-free dry-run path. No network, no creds.
