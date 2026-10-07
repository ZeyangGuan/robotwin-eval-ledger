"""Portable CSV, JSON and self-contained HTML reports (no network or JS)."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .ledger import Run, canonical, compare, summarize


def display(value):
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list)):
        return canonical(value)
    return str(value)


def esc(value):
    return html.escape(display(value), quote=True)


def write_csv(path, rows, fields):
    def cell(value):
        if value is None:
            return "unknown"
        if not isinstance(value, str):
            return canonical(value) if isinstance(value, (dict, list)) else value
        # Prevent spreadsheet applications from interpreting user text as formulas.
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: cell(v) for k, v in row.items() if k in fields})


CSS = """
:root{--ink:#172828;--muted:#61716f;--paper:#f4f4ed;--line:#d6ddd5;--green:#16765b;--gold:#aa702c;--red:#b45448}
*{box-sizing:border-box}body{margin:0;color:var(--ink);background:var(--paper);font:15px/1.55 system-ui,-apple-system,sans-serif}
main{max-width:1190px;margin:auto;padding:38px 30px 60px}header{border-top:4px solid var(--green);padding-top:21px;margin-bottom:28px}
.eyebrow{font-size:12px;letter-spacing:.13em;text-transform:uppercase;font-weight:750;color:var(--green)}
h1{font-size:clamp(30px,5vw,46px);line-height:1.1;letter-spacing:-.035em;margin:13px 0}h2{font-size:22px;margin:0 0 14px;letter-spacing:-.025em}
h3{font-size:16px;margin:0 0 10px}p{margin:8px 0}small,.muted{color:var(--muted)}.subtitle{font-size:17px;max-width:850px;color:var(--muted)}
.tag{display:inline-block;border:1px solid #c6d5ca;border-radius:30px;padding:3px 10px;background:#e8efe3;font-size:11px;font-weight:750;letter-spacing:.06em}
.banner{padding:16px 20px;background:#fff1d8;border-left:4px solid var(--gold);margin:20px 0}.note{background:#e7eee5;padding:15px 20px;margin:18px 0}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:22px 0}.card,section{background:#fffef9;border:1px solid var(--line);border-radius:8px;padding:20px}
.card .value{font-size:32px;line-height:1.2;font-weight:700;letter-spacing:-.035em;margin:9px 0}.card .label{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
section{margin:17px 0;padding:24px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid2 section{margin:0}.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;color:var(--muted);font-size:11px;letter-spacing:.03em;text-transform:uppercase;background:#f1f4ed}
th,td{padding:10px 11px;border-bottom:1px solid #e4e8df;vertical-align:top}td{overflow-wrap:anywhere}th:first-child{border-radius:4px 0 0 4px}code{font:12px ui-monospace,monospace}
.bar{display:flex;height:14px;background:#e8e9e2;border-radius:5px;overflow:hidden;margin:13px 0}.seg{min-width:0}.success{background:var(--green)}.fail{background:#99c3b0}.rejected{background:var(--gold)}.infra{background:var(--red)}.unfinished{background:#b5bfc5}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--muted)}.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
details{margin-top:16px}summary{cursor:pointer;font-weight:650}ul{padding-left:22px}.verdict{font-size:25px;font-weight:700;color:var(--green)}footer{margin-top:26px;color:var(--muted);font-size:12px}
@media(max-width:760px){main{padding:22px 14px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.grid2{grid-template-columns:1fr}section{padding:16px}.card{padding:14px}}
"""


def table(rows, fields):
    head = "".join(f"<th>{esc(label)}</th>" for _, label in fields)
    body = "".join("<tr>" + "".join(f"<td>{esc(row.get(k))}</td>" for k, _ in fields) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def card(label, value, detail):
    return f'<div class="card"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div><small>{esc(detail)}</small></div>'


def rate(counts):
    value = counts["observed_policy_rate"]
    return "unknown" if value is None else f"{value:.1%}"


def bar(counts):
    items = [("policy_success", "success", "Policy success"), ("policy_fail", "fail", "Policy fail"),
             ("expert_rejected", "rejected", "Expert rejected"), ("infra_error", "infra", "Infrastructure"),
             ("unfinished", "unfinished", "Unfinished")]
    total = counts["attempts"] or 1
    spans = "".join(f'<span class="seg {css}" style="width:{counts[key]/total*100:.5f}%" title="{label}: {counts[key]}"></span>' for key, css, label in items)
    legend = "".join(f'<span><i class="dot {css}"></i>{label} {counts[key]}</span>' for key, css, label in items)
    return f'<div class="bar" aria-label="Outcome distribution">{spans}</div><div class="legend">{legend}</div>'


def warnings(items):
    if not items:
        return '<p class="muted">No parser warnings. This does not prove every scheduled seed was logged.</p>'
    return '<div class="banner"><strong>Evidence warnings</strong><ul>' + ''.join(f'<li>{esc(w)}</li>' for w in items) + '</ul></div>'


def page(title, subtitle, body, *, synthetic):
    badge = '<span class="tag">SYNTHETIC DATA · NOT BENCHMARK RESULTS</span>' if synthetic else '<span class="tag">OBSERVED LEDGER EVIDENCE</span>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{esc(title)} | RoboTwin Eval Ledger</title><style>{CSS}</style></head>
<body><main><header><div class="eyebrow">RoboTwin Eval Ledger / v0.1</div><h1>{esc(title)}</h1><p class="subtitle">{esc(subtitle)}</p>{badge}</header>
{body}<footer>Offline, self-contained report · No telemetry, simulator dependency or automatic benchmark correction.<br>
Only observed events are represented. Missing events, unknown metadata and infrastructure failures are never converted into policy failures.</footer></main></body></html>'''


def summary_html(run, result):
    counts = result["latest_attempts"]
    completed = counts["policy_completed"]
    body = '<div class="cards">' + card("Observed policy rate", rate(counts), f'{counts["policy_success"]}/{completed} completed latest attempts')
    body += card("Expert rejected", counts["expert_rejected"], "Excluded from observed policy denominator")
    body += card("Unresolved latest", counts["infra_error"] + counts["unfinished"], "Infrastructure + unfinished; not policy fail")
    body += card("Retries", result["retry_seeds"], f'{result["superseded_attempts"]} superseded attempts retained') + '</div>'
    body += '<div class="note">Selection rule: highest declared attempt number per (task, seed). A newer unfinished retry supersedes an older success in this view. See all attempts below.</div>'
    body += '<section><h2>What happened to the sampled seeds?</h2>' + bar(counts)
    fields = [("scope", "Scope"), ("attempts", "Attempts"), ("expert_accepted", "Gate accepted"), ("expert_rejected", "Rejected"), ("expert_skipped", "Gate skipped"), ("expert_unknown", "Gate unknown"), ("policy_success", "Success"), ("policy_fail", "Fail"), ("infra_error", "Infra"), ("unfinished", "Unfinished")]
    body += table([dict(scope="Latest per seed", **counts), dict(scope="All attempts", **result["all_attempts"])], fields) + '</section>'
    body += '<section><h2>Official result, preserved separately</h2>'
    body += table(result["official"], [("task", "Task"), ("successes", "Numerator"), ("denominator", "Denominator"), ("label", "Recorded meaning")]) if result["official"] else '<p>unknown: no explicit upstream summary was recorded</p>'
    body += '<p class="muted">The ledger does not reconstruct, replace or “correct” the official denominator.</p></section>'
    body += '<section><h2>Variant coverage</h2><p class="muted">Latest attempts only. A variant value may describe multiple object slots. Unknown stays unknown.</p>'
    body += table(result["variants"], [("task", "Task"), ("variant", "Variant"), ("attempts", "Sampled"), ("expert_rejected", "Rejected"), ("policy_completed", "Policy completed"), ("policy_success", "Success"), ("infra_error", "Infra"), ("unfinished", "Unfinished")]) + '</section>'
    body += warnings(result["warnings"])
    body += f'<p class="muted">{result["workers"]} workers · {result["files"]} files · {result["duplicate_events"]} exact duplicate events ignored · {result["workers_without_end"]} workers without an end marker · {result["worker_errors"]} worker errors</p>'
    body += '<details><summary>Worker provenance</summary>' + table(list(run.workers.values()), [("worker_id", "Worker"), ("config_fingerprint", "Config fingerprint"), ("environment_fingerprint", "Environment fingerprint"), ("ended", "Ended")]) + '</details>'
    if run.worker_errors:
        body += '<details><summary>Worker errors</summary>' + table(run.worker_errors, [("worker_id", "Worker"), ("stage", "Stage"), ("reason", "Reason")]) + '</details>'
    body += '<details><summary>Full attempt ledger</summary>' + table([a.row() for a in run.attempts], [("task", "Task"), ("seed", "Seed"), ("attempt", "Attempt"), ("worker_id", "Worker"), ("expert", "Expert"), ("outcome", "Outcome"), ("error_stage", "Error stage"), ("error_reason", "Reason")]) + '</details>'
    return page(run.run_id, "Make the denominator, filtered variants, and interrupted attempts visible.", body, synthetic=result["synthetic"])


def comparison_html(left, right, result):
    ls, rs = summarize(left), summarize(right)
    body = '<div class="banner"><strong>Equal percentages do not imply equal evaluation sets.</strong> A shared task/seed is only a pairing candidate. Environment, variant, initial scene and instruction evidence must also be present and equal.</div>'
    body += '<div class="grid2">'
    for run, s in [(left, ls), (right, rs)]:
        c = s["latest_attempts"]
        body += f'<section><div class="eyebrow">{esc(run.run_id)}</div><h1>{rate(c)}</h1><p>{c["policy_success"]}/{c["policy_completed"]} observed completed policy attempts</p>' + bar(c)
        body += f'<p class="muted">{c["attempts"]} sampled · {c["expert_rejected"]} expert rejected</p></section>'
    body += '</div><div class="cards">' + card("Shared attempted seeds", result["shared_attempted_seeds"], "Intersection of task/seed keys")
    body += card("Shared policy seeds", result["shared_policy_seeds"], "Completed in both latest attempts")
    body += card("Metadata-matched", result["metadata_matched_pairs"], "Explicit environment + variant + scene + instruction")
    body += card("Unknown / mismatch", f'{result["pairing_unknown"]} / {result["pairing_mismatch"]}', "Incomplete or different pairing evidence") + '</div>'
    body += '<section><h2>Same observed policy evaluation set?</h2><p class="verdict">' + esc(result["same_observed_policy_set"]) + '</p>'
    n = result["metadata_matched_pairs"]
    body += f'<p>On the {n} metadata-matched pairs only: left {result["paired_left_success"]}/{n}, right {result["paired_right_success"]}/{n} successes.</p>' if n else '<p>No metadata-matched outcome pairs; no paired success difference is computed.</p>'
    body += '<p class="muted">This is a selected intersection, not a full benchmark comparison. Declared metadata equality cannot certify simulator determinism. Config fingerprints may differ between policies.</p></section>'
    body += warnings(result["warnings"])
    body += '<section><h2>Per-seed comparison</h2>' + table(result["rows"], [("task", "Task"), ("seed", "Seed"), ("left_attempt", "Left attempt"), ("right_attempt", "Right attempt"), ("left_outcome", "Left outcome"), ("right_outcome", "Right outcome"), ("pairing", "Pairing"), ("reason", "Evidence")]) + '</section>'
    return page("Same score. Which samples?", f'{left.run_id} versus {right.run_id} · latest declared attempt per task/seed', body, synthetic=result["synthetic"])


def write_summary(run: Run, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    result = summarize(run)
    (out / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary_rows = [dict(scope=k, **result[k]) for k in ("all_attempts", "latest_attempts")]
    write_csv(out / "summary.csv", summary_rows, list(summary_rows[0]))
    latest = run.latest()
    rows = [dict(a.row(), selected_latest=latest[a.key] is a) for a in run.attempts]
    write_csv(out / "attempts.csv", rows, list(rows[0]) if rows else ["task", "seed", "attempt", "outcome", "selected_latest"])
    fields = list(result["variants"][0]) if result["variants"] else ["task", "variant", "attempts"]
    write_csv(out / "variants.csv", result["variants"], fields)
    write_csv(out / "official.csv", result["official"], ["task", "successes", "denominator", "label"])
    (out / "report.html").write_text(summary_html(run, result), encoding="utf-8")
    return result


def write_comparison(left: Run, right: Run, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    result = compare(left, right)
    (out / "comparison.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    fields = ["task", "seed", "left_attempt", "right_attempt", "left_outcome", "right_outcome", "pairing", "reason"]
    write_csv(out / "comparison.csv", result["rows"], fields)
    (out / "report.html").write_text(comparison_html(left, right, result), encoding="utf-8")
    return result
