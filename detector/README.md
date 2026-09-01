# yantradetect — Fleet Incident Detector

Polls the `robots` table and maintains the `incidents` table: a robot
transitioning into `fault`/`estop` (per `yantracore.NOT_OPERATING`)
opens an incident (`sev` = `crit`/`serious`); transitioning back
patches it `Resolved` with duration and impact.

Detection follows the industry patterns in the spec: PagerDuty-style
dedup key (one open incident per robot, deterministic `INC-XXXX` ids so
retried upserts are idempotent), Prometheus-style pending window
(`--pending-polls`, default 2 consecutive abnormal polls) and clear
hold (`--clear-polls`), re-open window (`--reopen-window`, default
300 s) with flap counting, and stale auto-resolve when a robot vanishes
from telemetry (`--stale-polls`).

```
pip install -e . --break-system-packages
python -m yantradetect --interval 5          # poll loop
python -m yantradetect --once                # single poll
python -m yantradetect --once --dry-run      # print actions, write nothing
```

Layers:

* `yantradetect.engine.IncidentEngine` — pure: snapshots + `now` in,
  `Action`s out. No I/O, no clocks. `seed()` rebuilds dedup state from
  open rows after a restart.
* `yantradetect.sink.PostgRESTSink` — httpx writer (injectable client;
  tests use `httpx.MockTransport`, fully offline). `DryRunSink` prints.
* `yantradetect.__main__` — CLI glue.

```
python -m pytest tests -q   # offline
```
