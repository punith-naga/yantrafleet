# yantra-conform — a free VDA 5050 conformance tester

Point it at your MQTT broker. It watches your AGVs, sends them the standard
commands the specification says they must answer, and gives you a graded HTML
report you can send to your vendor.

No account. No sales call. No cloud service that needs your broker credentials.
It runs on your machine, against your broker, and the report is a single HTML
file you own.

```
$ yantra-conform run --broker mqtt://localhost:1883 --html report.html

A  100.0  uagv/v2/nexomotion/AMR_01   (47 pass / 0 fail / 0 warn / 5 n-a)
B   94.6  uagv/v2/nexomotion/AMR_04   (44 pass / 1 fail / 2 warn / 5 n-a)
       FAIL actions.terminal_reported   1 action(s) disappeared without a
                                        terminal status: a-AMR_04-4 (last RUNNING)
            fix: Keep a finished action in actionStates with its terminal status
            until the next order arrives ...

OVERALL B  score 97.3/100  (91 pass, 1 fail, 2 warn, 10 n/a)
grade capped by: actions.terminal_reported
HTML report: report.html
```

It grades **VDA 5050 v2.1**, in **52 checks** across connection, state, orders,
actionStates, instantActions, factsheet, visualization and protocol hygiene.
Every check names the clause it comes from and tells you what to change.

---

## Install

Python 3.10 or newer. Nothing else.

```bash
git clone https://github.com/yantrika-ai/yantrika.git
cd yantrika
pip install -e "tools/conformance[mqtt]"
```

`[mqtt]` pulls in `paho-mqtt`, which is needed only to talk to a broker.
Everything else — the rule set, replaying a capture, rendering a report — is
pure standard library and works without it.

Check it installed:

```bash
yantra-conform --version
yantra-conform checks          # print all 52 rules, no broker needed
```

No entry point on your PATH? `python3 -m yantraconform` works identically
everywhere below.

---

## The one command

```bash
yantra-conform run --broker mqtt://YOUR-BROKER:1883 --html report.html
```

Open `report.html` in any browser. That is the whole tool.

If your broker needs credentials or TLS:

```bash
yantra-conform run \
  --broker mqtts://fleet.example.com:8883 \
  --username fleetops --password '...' \
  --html report.html
```

If your fleet does not use the default `uagv` / `v2` topic prefix:

```bash
yantra-conform run --broker mqtt://localhost:1883 \
  --interface myfleet --major v2 --html report.html
```

### Is it safe to run against a live fleet?

By default **no** — and it says so plainly, because it is an *active* tester.
It publishes real `order` and `instantActions` messages: a `cancelOrder`, a
`stateRequest`, a `factsheetRequest`, an `initPosition`, and four orders. It is
deliberately conservative — it only ever dispatches a vehicle to a node that
vehicle itself reported, and `initPosition` re-asserts the pose the vehicle
already believes it has — but it *will* interrupt work in progress.

Run it against a vehicle on a test stand, or use passive mode:

```bash
yantra-conform run --broker mqtt://localhost:1883 --passive --html report.html
```

`--passive` publishes **nothing**. It grades everything observable from the
wire alone — the state topic, headers, timestamps, identity, retention, topic
hygiene, the factsheet — and reports the command-surface checks as *not
applicable* rather than passing them on no evidence.

Scope it to one vehicle while you are testing:

```bash
yantra-conform run --broker mqtt://localhost:1883 \
  --manufacturer acme --serial AGV_01 --html report.html
```

---

## Hand over a capture instead of your broker

This is the path for anyone who cannot give an outsider broker access — which
is most people. A vendor's broker sits inside a customer's plant network; an
integrator's sits behind a VPN and a change-control ticket.

The tool separates **collecting** evidence (needs the broker, takes a minute)
from **grading** it (needs nothing at all).

**On your network**, record the session:

```bash
yantra-conform run --broker mqtt://localhost:1883 \
  --capture capture.json --html report.html
```

**Read `capture.json` before you send it.** It is plain, indented JSON. It
contains exactly the MQTT messages that crossed the wire and the probes the
tester sent — nothing else. No broker hostname credentials, no environment, no
files. Every byte in it came off your own network and you can check that:

```json
{
  "kind": "yantra-conform-capture",
  "messages": [
    {"t": 0.41, "topic": "uagv/v2/acme/AGV_01/state",
     "payload": "{\"headerId\": 12, ...}", "qos": 0, "retain": false}
  ]
}
```

**Anywhere else** — a laptop with no network, your vendor's desk, a CI job —
grade it offline:

```bash
yantra-conform replay capture.json --html report.html
```

The replayed report is **identical** to the live one, check for check, string
for string, including the order and instantAction findings. It is a re-grade of
the same evidence, not a summary of it. That means you can also re-grade an old
capture against a newer rule set to see what a stricter version of the tool
would have said.

---

## Wire it into CI

`--fail-under` sets a floor. Exit code `1` means the score was below it, `2`
means the run could not be performed at all (no broker, no vehicles, bad
arguments), `0` means it passed.

```bash
yantra-conform run --broker mqtt://localhost:1883 \
  --json report.json --html report.html \
  --fail-under 90 --quiet
```

A regression gate on a capture, with no broker in CI at all:

```bash
yantra-conform replay capture.json --fail-under 90 --quiet
```

---

## Reading the report

Every finding carries four things, because a red label on its own is not
actionable:

| | |
|---|---|
| **Spec** | the clause, e.g. `VDA 5050 2.1 §6.5` |
| **Expected** | what the specification requires, in one line |
| **Observed** | what your vehicle actually did, with counts and examples |
| **How to fix** | what to change in the firmware, in plain English |

**Severity** is a weight: `critical` 10, `major` 5, `minor` 2, `info` 0.
**Status** is a multiplier: pass 1.0, warn 0.5, fail 0.0. Checks whose evidence
was never observed are `skip` — excluded from both sides of the ratio rather
than counted against you.

```
score = 100 × Σ(weight × credit) ⁄ Σ(weight)
```

Then two caps are applied on top of the letter grade, because a weighted
average alone lets a vehicle with one fatal defect still look like a B:

* any failed **critical** check caps the grade at **F**
* any failed **major** check caps the grade at **B**

The report always names which checks capped the grade, so nobody has to
reverse-engineer why an 89 came out as a B.

### "n/a" is not a pass

A `skip` means the tester never saw the evidence a check needs, and it always
says why. A run full of skips is a run that did not exercise much — check the
warnings at the top of the report. Two skips are normal and expected:

* `connection.lastwill` — a last-will is registered *inside the broker* and is
  not visible to another MQTT client. Verify it by pulling the vehicle's
  network cable and watching its connection topic.
* `visualization.wellformed` — the visualization topic is optional.

The `order.*` checks also skip when **another master control is dispatching the
same vehicle**: if the fleet manager gives the AGV a new order while the
tester's probe is in flight, the order being updated is no longer the order in
progress, and the result is genuinely unevaluable. The tester says exactly that
rather than guessing. To grade order handling properly, test a vehicle nothing
else is driving.

### One defect, one finding

A single defect should cost a vehicle once, not fifteen times. When the
evidence a family of checks depends on is entirely absent, one check *fails*
for the absence and the rest report `skip`: a vehicle that never publishes
`state` fails `state.published` and skips the other state checks, rather than
scoring a misleading zero on nineteen things that were never observed.

---

## Commands

| Command | What it does |
|---|---|
| `run` | connect to a broker, test what is there, write the report |
| `replay` | grade a captured session offline — no broker |
| `render` | re-render a saved JSON report as HTML |
| `checks` | print the whole rule set (add `--json`) — no broker |

Useful flags on `run`: `--passive`, `--manufacturer`, `--serial`, `--node`,
`--interface`, `--major`, `--discover-seconds`, `--observe-seconds`,
`--settle-seconds`, `--retain-seconds`, `--max-robots`, `--capture`, `--json`,
`--html`, `--title`, `--fail-under`, `--quiet`. Run `yantra-conform run --help`
for the full list.

---

## Use it as a library

```python
from yantraconform import RunOptions, run_conformance
from yantraconform.session import PahoSession, parse_broker
from yantraconform.report_html import render_html

session = PahoSession(parse_broker("mqtt://localhost:1883"))
try:
    report = run_conformance(session, RunOptions(observe_seconds=10))
finally:
    session.close()

document = report.to_dict()
print(document["summary"]["grade"], document["summary"]["score"])
open("report.html", "w").write(render_html(document))
```

The report document is stable and versioned (`schema_version`, currently
`"1.0"`):

```jsonc
{
  "schema_version": "1.0",
  "tool": "yantra-conform", "tool_version": "0.1.0",
  "spec": "VDA 5050 2.1.0",
  "generated_at": "...", "broker": "...", "options": { },
  "summary": {
    "robots": 2, "score": 97.3, "grade": "B",
    "grade_capped_by": ["actions.terminal_reported"],
    "counts": {"pass": 91, "fail": 1, "warn": 2, "skip": 10},
    "by_category": { }, "failed_checks": [ ]
  },
  "warnings": [ ],
  "robots": [{
    "manufacturer": "...", "serial": "...", "topic_prefix": "...",
    "score": 94.6, "grade": "B", "counts": { },
    "message_counts": { }, "topics_seen": [ ], "notes": [ ],
    "checks": [{
      "id": "actions.terminal_reported", "title": "...", "category": "actions",
      "severity": "major", "spec_ref": "VDA 5050 2.1 §6.9", "status": "fail",
      "expected": "...", "observed": "...", "remediation": "...", "detail": { }
    }]
  }]
}
```

`yantra-conform checks --json` emits the rule set on its own, in the same
shape minus the result fields — that is what a "what we test" page renders.

---

## Why there is no web version

A browser cannot open a raw MQTT/TCP connection. Nothing in a web page can talk
to your broker without you first standing up a websocket listener and handing
a website your credentials, which is a worse trade than running a local
command. So the honest split is:

* **Testing your fleet** is a local command. It stays that way.
* **Everything that is not testing your fleet** works fine on the web: browsing
  the rule set, reading a sample report, and — because a capture is just a JSON
  file — dropping a capture in to have it graded and rendered.

---

## Development

```bash
python3 -m pytest tools/conformance -q
```

The suite grades a purpose-built non-compliant robot
(`tests/fake_robots.py`) and this repo's real simulator (`sim/yantrasim`).
Every check in the catalogue has a test that makes it **fail on purpose**, and
`test_every_check_has_a_failing_case` fails the build if a check is ever added
without one — a conformance tester that cannot be shown failing is worthless.

The networked tests start a local `mosquitto` if one is installed and skip
cleanly if not. To run them against a broker you already have:

```bash
YANTRACONFORM_TEST_BROKER=localhost:1883 python3 -m pytest tools/conformance -q
```

---

## Corrections welcome

Where this tool and the specification disagree, the specification wins. Every
check names its clause so a disagreement is a short conversation. Open an issue
with the clause and the payload and the rule gets fixed.

Part of [Yantrika](https://yantrika.ai/) — open-source, multi-vendor AMR
fleet software. MIT licensed.
