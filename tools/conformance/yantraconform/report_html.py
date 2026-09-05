"""Single-file HTML report renderer.

No build step, no framework, no external asset of any kind: the output is one
self-contained ``.html`` file an integrator can email to a vendor. The palette
and typography are lifted from ``marketing/assets/styles.css`` (the same
``--bg``/``--ink``/``--accent`` tokens) so a report opened from the marketing
site looks like it belongs there, without importing that stylesheet -- a
report that only renders correctly when it can reach a web server is not a
report you can attach to a procurement email.
"""
from __future__ import annotations

import html
import json
from typing import Any

from .model import GRADE_BANDS, SEVERITY_WEIGHT

STATUS_LABEL = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}
CATEGORY_TITLES = {
    "connection": "Connection topic",
    "state": "State topic",
    "order": "Order handling",
    "actions": "actionStates lifecycle",
    "instant": "instantActions",
    "factsheet": "Factsheet",
    "visualization": "Visualization topic",
    "protocol": "Protocol hygiene",
}
CATEGORY_ORDER = ("connection", "state", "order", "actions", "instant",
                  "factsheet", "visualization", "protocol")

_CSS = """
:root{
  --bg:#fbfaf7; --surface:#ffffff; --surface2:#f4f2ec;
  --border:#e4e0d6; --border-strong:#d3cebe;
  --ink:#1c1b17; --muted:#5b584e; --dim:#8a8678;
  --accent:#2563c9; --accent-dim:#e8f0fd;
  --good:#0d8a4f; --good-dim:#e4f4ec; --warn:#b3720a; --warn-dim:#fbf1dd;
  --bad:#b3261e; --bad-dim:#fbe6e4;
  --r:10px; --r-sm:6px;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --font:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
html{color-scheme:light}
body{margin:0;background:var(--bg);color:var(--ink);
     font:16px/1.6 var(--font);-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:0 24px}
h1,h2,h3{line-height:1.22;font-weight:750;letter-spacing:-.01em;margin:0}
h1{font-size:clamp(26px,4vw,38px)}
h2{font-size:22px;margin:0 0 12px}
h3{font-size:16px}
p{margin:0 0 14px}
a{color:var(--accent)}
code,.mono{font-family:var(--mono);font-size:13px}
header.rep{background:var(--ink);color:#eae7dc;padding:34px 0 30px}
header.rep h1{color:#fff}
header.rep .kicker{font-size:12.5px;font-weight:700;letter-spacing:.09em;
  text-transform:uppercase;color:#9ec5f4;margin:0 0 10px}
header.rep .meta{color:#c9c5b6;font-size:13.5px;margin-top:14px}
header.rep .meta span{margin-right:18px;white-space:nowrap}
.gradeblock{display:flex;align-items:center;gap:22px;flex-wrap:wrap;margin-top:22px}
.grade{width:96px;height:96px;border-radius:20px;display:grid;place-items:center;
  font-size:46px;font-weight:800;color:#fff;flex:none}
.grade.A{background:#0d8a4f}.grade.B{background:#3f8f2f}
.grade.C{background:#b3720a}.grade.D{background:#c2521c}.grade.F{background:#b3261e}
.score{font-size:15px;color:#c9c5b6}
.score strong{color:#fff;font-size:26px;font-weight:800}
.tallies{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.tally{border-radius:999px;padding:3px 12px;font-size:12.5px;font-weight:700;
  background:rgba(255,255,255,.12);color:#eae7dc}
.tally.pass{background:var(--good);color:#fff}
.tally.fail{background:var(--bad);color:#fff}
.tally.warn{background:var(--warn);color:#fff}
section{padding:34px 0}
section.tight{padding:22px 0}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:20px 22px;margin:0 0 16px}
.robot-head{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:6px}
.robot-head .id{font-family:var(--mono);font-size:15px;font-weight:700}
.pill{border:1px solid var(--border-strong);border-radius:999px;padding:3px 11px;
  font-size:12px;color:var(--muted);white-space:nowrap}
.pill.g{border-color:transparent;color:#fff;font-weight:700}
.pill.g.A{background:#0d8a4f}.pill.g.B{background:#3f8f2f}
.pill.g.C{background:#b3720a}.pill.g.D{background:#c2521c}.pill.g.F{background:#b3261e}
.cat{margin:22px 0 0}
.cat h3{font-size:13px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--dim);margin:0 0 8px}
details.chk{border:1px solid var(--border);border-radius:var(--r-sm);
  margin:0 0 7px;background:var(--surface)}
details.chk[data-status="fail"]{border-color:#e8b5b1;background:var(--bad-dim)}
details.chk[data-status="warn"]{border-color:#e8d3a6;background:var(--warn-dim)}
details.chk>summary{list-style:none;cursor:pointer;padding:10px 14px;
  display:flex;align-items:center;gap:12px}
details.chk>summary::-webkit-details-marker{display:none}
.badge{font-size:10.5px;font-weight:800;letter-spacing:.06em;border-radius:4px;
  padding:3px 7px;flex:none;min-width:46px;text-align:center}
.badge.pass{background:var(--good-dim);color:var(--good)}
.badge.fail{background:#f6cecb;color:var(--bad)}
.badge.warn{background:#f4e3c0;color:var(--warn)}
.badge.skip{background:var(--surface2);color:var(--dim)}
summary .title{font-weight:650;font-size:14.5px;flex:1;min-width:0}
summary .sev{font-size:11.5px;color:var(--dim);text-transform:uppercase;
  letter-spacing:.05em;flex:none}
summary .cid{font-family:var(--mono);font-size:11.5px;color:var(--dim);flex:none}
.body{padding:2px 14px 14px 72px;font-size:14px}
.body dl{margin:0;display:grid;grid-template-columns:max-content 1fr;
  gap:4px 14px;align-items:baseline}
.body dt{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--dim);font-weight:700}
.body dd{margin:0}
.body .fix{margin-top:10px;padding:10px 12px;border-left:3px solid var(--accent);
  background:var(--accent-dim);border-radius:0 var(--r-sm) var(--r-sm) 0;
  font-size:13.5px}
.body pre{background:#1c1b17;color:#eae7dc;border-radius:var(--r-sm);
  padding:10px 12px;overflow-x:auto;font-size:12px;margin:10px 0 0}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}
.controls button{font:inherit;font-size:13px;font-weight:700;cursor:pointer;
  border:1px solid var(--border-strong);background:var(--surface);
  color:var(--muted);border-radius:999px;padding:6px 14px}
.controls button[aria-pressed="true"]{background:var(--ink);color:#fff;
  border-color:var(--ink)}
table.sum{width:100%;border-collapse:collapse;font-size:14px}
table.sum th,table.sum td{border:1px solid var(--border);padding:8px 11px;
  text-align:left}
table.sum th{background:var(--surface2);font-weight:700}
table.sum td.n{text-align:right;font-family:var(--mono);font-size:13px}
.note{background:var(--surface2);border:1px solid var(--border);
  border-radius:var(--r-sm);padding:12px 14px;font-size:13.5px;color:var(--muted);
  margin:0 0 14px}
footer.rep{border-top:1px solid var(--border);padding:28px 0 40px;margin-top:24px;
  color:var(--dim);font-size:13px}
footer.rep strong{color:var(--ink)}
@media print{
  .controls{display:none}
  details.chk{break-inside:avoid}
  details.chk>.body{display:block!important}
  header.rep{background:#fff;color:var(--ink)}
  header.rep h1,header.rep .meta,header.rep .score,header.rep .score strong{color:var(--ink)}
}
"""

_JS = """
(function(){
  var buttons = document.querySelectorAll('.controls button');
  function apply(filter){
    document.querySelectorAll('details.chk').forEach(function(el){
      var s = el.getAttribute('data-status');
      var show = filter === 'all'
        || (filter === 'problems' && (s === 'fail' || s === 'warn'))
        || filter === s;
      el.hidden = !show;
    });
    document.querySelectorAll('.cat').forEach(function(cat){
      var any = cat.querySelectorAll('details.chk:not([hidden])').length;
      cat.hidden = any === 0;
    });
    buttons.forEach(function(b){
      b.setAttribute('aria-pressed', String(b.dataset.filter === filter));
    });
  }
  buttons.forEach(function(b){
    b.addEventListener('click', function(){ apply(b.dataset.filter); });
  });
  apply('all');
})();
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _grade_note(grade: str, capped_by: list[str]) -> str:
    if capped_by:
        return ("Grade capped at " + grade + " by "
                + ", ".join(sorted(capped_by))
                + " — a failed check at this severity limits the grade "
                  "regardless of the weighted score.")
    for floor, g in GRADE_BANDS:
        if g == grade:
            return f"Grade {grade}: score at or above {floor:g}."
    return ""


def _check_html(chk: dict[str, Any]) -> str:
    status = chk["status"]
    detail_pre = ""
    if chk.get("detail"):
        detail_pre = ("<pre>" + _e(json.dumps(chk["detail"], indent=2,
                                              sort_keys=True, default=str))
                      + "</pre>")
    fix = ""
    if chk.get("remediation"):
        fix = f'<div class="fix"><strong>How to fix:</strong> {_e(chk["remediation"])}</div>'
    return f"""
      <details class="chk" data-status="{_e(status)}">
        <summary>
          <span class="badge {_e(status)}">{_e(STATUS_LABEL.get(status, status))}</span>
          <span class="title">{_e(chk['title'])}</span>
          <span class="sev">{_e(chk['severity'])}</span>
          <span class="cid">{_e(chk['id'])}</span>
        </summary>
        <div class="body">
          <dl>
            <dt>Expected</dt><dd>{_e(chk['expected'])}</dd>
            <dt>Observed</dt><dd>{_e(chk['observed'])}</dd>
            <dt>Spec</dt><dd>{_e(chk['spec_ref'])}</dd>
          </dl>
          {fix}
          {detail_pre}
        </div>
      </details>"""


def _robot_html(robot: dict[str, Any]) -> str:
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for chk in robot["checks"]:
        by_cat.setdefault(chk["category"], []).append(chk)
    order = [c for c in CATEGORY_ORDER if c in by_cat]
    order += [c for c in sorted(by_cat) if c not in CATEGORY_ORDER]
    cats = "".join(
        f'<div class="cat"><h3>{_e(CATEGORY_TITLES.get(c, c))}</h3>'
        + "".join(_check_html(chk) for chk in by_cat[c]) + "</div>"
        for c in order)
    counts = robot["counts"]
    notes = "".join(f'<div class="note">{_e(n)}</div>' for n in robot["notes"])
    msg = robot["message_counts"]
    msg_pills = "".join(
        f'<span class="pill">{_e(k)} {_e(v)}</span>'
        for k, v in sorted(msg.items()) if v)
    return f"""
    <div class="card">
      <div class="robot-head">
        <span class="pill g {_e(robot['grade'])}">{_e(robot['grade'])}</span>
        <span class="id">{_e(robot['topic_prefix'])}</span>
        <span class="pill">score {_e(robot['score'])}</span>
        <span class="pill">VDA {_e(robot['version_reported'] or 'unknown')}</span>
        {msg_pills}
      </div>
      <p class="note" style="margin-top:14px">
        {_e(counts['pass'])} passed &middot; {_e(counts['fail'])} failed &middot;
        {_e(counts['warn'])} warnings &middot; {_e(counts['skip'])} not applicable.
        {_e(_grade_note(robot['grade'], robot['grade_capped_by']))}
      </p>
      {notes}
      {cats}
    </div>"""


def render_html(document: dict[str, Any], title: str | None = None) -> str:
    """Render a report dict (see :meth:`Report.to_dict`) as one HTML page."""
    summary = document["summary"]
    counts = summary["counts"]
    grade = summary["grade"]
    robots = "".join(_robot_html(r) for r in document["robots"])
    warnings = "".join(f'<div class="note">{_e(w)}</div>'
                       for w in document.get("warnings") or [])
    rows = "".join(
        "<tr><td>{}</td><td class='n'>{}</td><td class='n'>{}</td>"
        "<td class='n'>{}</td><td class='n'>{}</td></tr>".format(
            _e(CATEGORY_TITLES.get(cat, cat)), _e(c["pass"]), _e(c["fail"]),
            _e(c["warn"]), _e(c["skip"]))
        for cat, c in sorted(summary["by_category"].items(),
                             key=lambda kv: (CATEGORY_ORDER.index(kv[0])
                                             if kv[0] in CATEGORY_ORDER else 99)))
    heading = title or "VDA 5050 conformance report"
    weights = ", ".join(f"{k} {v}" for k, v in SEVERITY_WEIGHT.items() if v)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(heading)} — {_e(document['broker'] or 'offline capture')}</title>
<meta name="robots" content="noindex">
<style>{_CSS}</style>
</head><body>
<header class="rep"><div class="wrap">
  <p class="kicker">{_e(document['tool'])} {_e(document['tool_version'])} &middot; {_e(document['spec'])}</p>
  <h1>{_e(heading)}</h1>
  <div class="gradeblock">
    <div class="grade {_e(grade)}">{_e(grade)}</div>
    <div>
      <div class="score"><strong>{_e(summary['score'])}</strong> / 100 across
        {_e(summary['robots'])} vehicle(s), {_e(sum(counts.values()))} checks</div>
      <div class="tallies">
        <span class="tally pass">{_e(counts['pass'])} pass</span>
        <span class="tally fail">{_e(counts['fail'])} fail</span>
        <span class="tally warn">{_e(counts['warn'])} warn</span>
        <span class="tally">{_e(counts['skip'])} n/a</span>
      </div>
    </div>
  </div>
  <p class="meta">
    <span>Broker: {_e(document['broker'] or 'offline capture')}</span>
    <span>Generated: {_e(document['generated_at'])}</span>
    <span>Schema: {_e(document['schema_version'])}</span>
  </p>
</div></header>

<section class="tight"><div class="wrap">
  {warnings}
  <div class="note">
    Scoring: every check carries a severity weight ({_e(weights)}); pass scores
    full, warn scores half, fail scores nothing, and checks whose evidence was
    never observed are excluded rather than counted against the vehicle.
    {_e(_grade_note(grade, summary['grade_capped_by']))}
  </div>
  <div class="tbl-wrap"><table class="sum">
    <thead><tr><th>Area</th><th>Pass</th><th>Fail</th><th>Warn</th><th>N/A</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
</div></section>

<section class="tight"><div class="wrap">
  <h2>Findings</h2>
  <div class="controls">
    <button data-filter="all" aria-pressed="true">All</button>
    <button data-filter="problems" aria-pressed="false">Problems only</button>
    <button data-filter="fail" aria-pressed="false">Failures</button>
    <button data-filter="warn" aria-pressed="false">Warnings</button>
    <button data-filter="skip" aria-pressed="false">Not applicable</button>
  </div>
  {robots}
</div></section>

<footer class="rep"><div class="wrap">
  <p><strong>{_e(document['tool'])}</strong> is a free, open-source VDA 5050
  conformance tester. It observes and probes a vehicle over MQTT and grades
  what it actually saw — it does not read your source, and it never invents a
  position or a node id it was not told about.</p>
  <p>Every check above names the clause it comes from. Where this report and
  the specification disagree, the specification wins: open an issue and the
  rule gets fixed.</p>
</div></footer>
<script>{_JS}</script>
</body></html>
"""
