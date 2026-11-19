"""Merge Stack Canary results and publish a static matrix and local drafts."""

import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from . import __version__, config, repro
from .checks import CHECKS
from .harness_hermes import b6_summary, classify_attempt
from .releases import version_key
from .runtime_ollama import canary_home, safe_path

OUTCOMES = ("pass", "fail", "flaky", "timeout", "error", "n/a")
DRAFT_OUTCOMES = {"fail", "flaky", "timeout"}
CHECK_IDS = (*CHECKS, "B6")
REPO = "https://github.com/Lil-Clanky/stack-canary"
NAMES = {**CHECKS, "B6": "Hermes Agent file write"}
# The canary mark, drawn for a 64 by 64 viewBox.
BIRD = ('<path fill="#eab308" d="M19 41 4 51l5-15z"/>'
        '<ellipse fill="#facc15" cx="29" cy="38" rx="17" ry="12.5" transform="rotate(-18 29 38)"/>'
        '<circle fill="#facc15" cx="42" cy="23" r="10.5"/>'
        '<path fill="#eab308" d="M16 39c5-8 15-10 23-5-4 8-13 11-23 5z"/>'
        '<path fill="#f97316" d="m51 18 10 5-10 4z"/><circle fill="#0f172a" cx="45" cy="21" r="2"/>'
        '<path fill="none" stroke="#f97316" stroke-width="2.5" stroke-linecap="round" d="M27 50v7m8-8v8"/>')
LOGO = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" aria-hidden="true">{BIRD}</svg>'
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SVG_COLORS = {"pass": "#4ade80", "fail": "#f87171", "flaky": "#fbbf24", "timeout": "#c084fc",
              "error": "#fb923c", "n/a": "#64748b"}
# Stroke icons centered on the origin, so a tile reads without relying on color.
SVG_ICONS = {"pass": "M-4.5 0l3 3 6-6.5", "fail": "M-3.5-3.5l7 7m0-7l-7 7", "flaky": "M-5 1c2-4 3-4 5 0s3 4 5 0",
             "timeout": "M0-4.5V0l3 2", "error": "M0-5v5.5m0 3.5v.5", "n/a": "M-4 0h8"}
STYLE = """
:root {
  color-scheme: light dark;
  --bg:#f8fafc; --panel:#fff; --text:#0f172a; --muted:#475569; --line:#dbe2ea; --link:#17549a;
  --pass:#117848; --fail:#b8293d; --flaky:#925500; --timeout:#7638a6; --error:#a33a13; --na:#596879;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#020617; --panel:#0f172a; --text:#f8fafc; --muted:#a3b1c6; --line:#27344a; --link:#a5d0ff;
          --pass:#82dda7; --fail:#ff9ea8; --flaky:#f9c16d; --timeout:#d8adff; --error:#ffb394; --na:#bfccd9; }
}
* { box-sizing:border-box }
body { margin:0; font:16px/1.55 system-ui,sans-serif; background:var(--bg); color:var(--text) }
body::before { content:""; position:absolute; inset:0 0 auto; height:26rem; z-index:-1;
  background:radial-gradient(var(--line) 1px,transparent 1.5px) 0 0/22px 22px;
  -webkit-mask-image:linear-gradient(#000,transparent); mask-image:linear-gradient(#000,transparent) }
header,main,footer { max-width:70rem; margin:0 auto; padding:0 clamp(1rem,4vw,2rem) }
table,pre,.outcome,.release { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace }
a { color:var(--link) }
a:focus-visible,.scroll:focus-visible { outline:2px solid var(--link); outline-offset:2px }
header { display:flex; align-items:center; justify-content:space-between; padding-block:1.1rem }
.brand { display:flex; align-items:center; gap:.6rem; font-weight:700; font-size:1.1rem; color:inherit; text-decoration:none }
.brand svg { width:2.1rem; height:2.1rem }
.button { padding:.45rem 1rem; border-radius:.5rem; background:#facc15; color:#0f172a; font-weight:650; text-decoration:none }
.button:hover { background:#fde047 }
.eyebrow { margin:2.5rem 0 0; font-size:.78rem; font-weight:650; letter-spacing:.08em; text-transform:uppercase; color:var(--muted) }
h1 { margin:.4rem 0 .9rem; max-width:18ch; font-size:clamp(2rem,5.5vw,3.2rem); line-height:1.06; letter-spacing:-.03em; text-wrap:balance }
h2 { margin:2.75rem 0 .8rem; font-size:1.25rem; letter-spacing:-.01em }
.lede { margin:0; max-width:62ch; font-size:1.1rem; color:var(--muted) }
.summary,.scroll,.meta,pre { border:1px solid var(--line); border-radius:.9rem; background:var(--panel) }
.summary { margin-top:2rem; padding:1.25rem 1.4rem }
.summary .eyebrow { margin:0 }
.headline { display:flex; flex-wrap:wrap; align-items:end; justify-content:space-between; gap:.5rem 2rem }
.release { margin:.1rem 0 0; font-size:1.7rem; font-weight:700; letter-spacing:-.02em }
.verdict { margin:0; font-size:1.1rem; font-weight:700 }
.legend,.findings { display:flex; flex-wrap:wrap; gap:.5rem; margin:.9rem 0 0; padding:0; list-style:none }
.findings { flex-direction:column; padding-top:.9rem; border-top:1px solid var(--line) }
.findings li { display:flex; flex-wrap:wrap; align-items:center; gap:.3rem .7rem }
.scroll { overflow-x:auto }
table { border-collapse:collapse; width:100%; text-align:left; font-size:.85rem }
th,td { padding:.6rem .7rem; border-top:1px solid var(--line); vertical-align:top }
thead th { border-top:0; vertical-align:bottom; white-space:nowrap }
thead small,.details,td small { display:block; font:400 .72rem/1.35 system-ui,sans-serif; color:var(--muted); white-space:normal }
thead small { max-width:6.5rem }
tbody { border-top:2px solid var(--line) }
th[scope=rowgroup] { font-size:1rem; white-space:nowrap }
tbody:first-of-type th[scope=rowgroup]::after { content:"newest"; display:block; width:fit-content; margin-top:.3rem; padding:0 .45rem;
  border-radius:99px; background:#facc15; color:#0f172a; font:650 .68rem/1.6 system-ui,sans-serif }
th[scope=row] { white-space:nowrap }
.details { min-width:15rem; margin-top:.15rem }
@media (max-width:44rem) { .details { display:none } th,td { padding:.5rem .5rem } }
tbody tr:hover > td,tbody tr:hover > th[scope=row] { background:color-mix(in srgb,var(--text) 4%,transparent) }
td > * { display:block; width:fit-content }
td small { max-width:9rem; margin-top:.25rem; overflow-wrap:anywhere }
td a { margin-top:.25rem; font-size:.75rem; white-space:nowrap }
.outcome { display:flex; align-items:center; gap:.3rem; width:fit-content; padding:.05rem .55rem; border-radius:99px;
  background:color-mix(in srgb,currentColor 13%,transparent); font-size:.78rem; font-weight:700; white-space:nowrap }
.outcome::before { content:"\\2713" } .outcome.fail::before { content:"\\2715" } .outcome.flaky::before { content:"~" }
.outcome.timeout::before { content:"\\2026" } .outcome.error::before { content:"!" } .outcome.na::before { content:"\\2013" }
.pass { color:var(--pass) } .fail { color:var(--fail) } .flaky { color:var(--flaky) }
.timeout { color:var(--timeout) } .error { color:var(--error) } .na,.empty { color:var(--na) }
.attempts { margin:.3rem 0 0 .55rem; font-size:.5rem; letter-spacing:.3em; line-height:1 }
.attempts span { display:inline }
.regression,.harness-failure { margin-top:.3rem; font:700 .68rem/1.4 system-ui,sans-serif; letter-spacing:.05em; text-transform:uppercase }
.regression { color:var(--fail) }
.harness-failure { color:var(--timeout) }
.meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(13rem,1fr)); gap:1rem 1.5rem; margin:0; padding:1.2rem 1.4rem }
.meta dt { font-size:.72rem; font-weight:650; letter-spacing:.06em; text-transform:uppercase; color:var(--muted) }
.meta dd { margin:0; font-weight:600; overflow-wrap:anywhere }
pre { margin:0; padding:1.1rem 1.3rem; overflow-x:auto; font-size:.85rem; line-height:1.7 }
footer { display:flex; flex-wrap:wrap; justify-content:space-between; gap:.5rem 1.5rem; margin-top:3rem; padding-block:1.4rem 2.5rem;
  border-top:1px solid var(--line); font-size:.85rem; color:var(--muted) }
"""


@dataclass
class Run:
    version: str
    timestamp: datetime
    data: dict


@dataclass
class Cell:
    run: Run
    model: dict
    record: dict
    tag: str
    harness: dict

    @property
    def outcome(self):
        return self.record["outcome"]


def _version_order(version):
    try:
        return (1, version_key(version.removeprefix("v")), version)
    except ValueError:
        return (0, (), version)


def _timestamp(path, data):
    stamp = data.get("timestamp")
    if isinstance(stamp, str):
        try:
            value = datetime.fromisoformat(stamp)
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        except ValueError:
            pass
    if match := re.match(r"\d{8}T\d{6}(?:\.\d{1,6})?Z", path.name):
        stamp = match[0]
        fmt = "%Y%m%dT%H%M%S.%fZ" if "." in stamp else "%Y%m%dT%H%M%SZ"
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def load_runs(directories):
    paths = {path.resolve() for directory in directories for path in directory.glob("*.json")}
    runs = []
    for path in sorted(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                continue
            runtime = data.get("runtime") or {}
            version = (runtime.get("version") if isinstance(runtime, dict) else None) or data.get("runtime_version")
            if isinstance(version, str) and version:
                runs.append(Run(version.removeprefix("v"), _timestamp(path, data), data))
        except (OSError, ValueError, RecursionError) as error:
            print(f"canary: skipping {path}: {error}", file=sys.stderr)
    return sorted(runs, key=lambda run: run.timestamp)


def _harness_records(data):
    info = data.get("harness") or {}
    entries = info.get("releases", [info]) if isinstance(info, dict) else info
    return {entry["tag"]: entry for entry in entries or []
            if isinstance(entry, dict) and isinstance(entry.get("tag"), str)}


def _error_message(*errors):
    for error in errors:
        if isinstance(error, dict):
            return error.get("message") or error.get("code") or "Infrastructure error"
        if isinstance(error, str) and error:
            return error
    return ""


def _remember(cells, cell):
    key = (cell.run.version, cell.model["name"], cell.record["check_id"], cell.tag)
    if cell.outcome != "error" or key not in cells or cells[key].outcome == "error":
        cells[key] = cell


def _normalize_b6(record):
    attempts = record.get("attempts") or []
    if not attempts or any(item.get("outcome") not in {"pass", "fail", "timeout", "error"} for item in attempts):
        return record
    attempts = [classify_attempt(item) for item in attempts]
    return {**record, **b6_summary(attempts), "attempts": attempts}


def merge(runs):
    cells, models, harnesses = {}, {}, {}
    for run in runs:
        selected = (run.data.get("settings") or {}).get("checks") or CHECK_IDS
        installed = _harness_records(run.data)
        harnesses.update(installed)
        issues = {issue.get("tag", ""): issue for issue in run.data.get("harness_errors") or []}
        requested_tags = installed.keys() | issues.keys() | set(run.data.get("hermes_tags") or [])
        for model in run.data["models"]:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                continue
            key = (run.version, model["name"])
            records = [
                _normalize_b6(record) if record.get("check_id") == "B6" else record
                for record in model.get("checks") or []
            ]
            if key not in models or any(
                record.get("outcome") in OUTCOMES and record["outcome"] != "error" for record in records
            ):
                models[key] = model
            seen = set()
            for record in records:
                check = record.get("check_id")
                if check not in CHECK_IDS or record.get("outcome") not in OUTCOMES:
                    continue
                default_tag = next(iter(requested_tags)) if len(requested_tags) == 1 else ""
                tag = (record.get("harness_tag") or default_tag) if check == "B6" else ""
                if check == "B6" and not tag and record["outcome"] in {"n/a", "error"} and requested_tags:
                    tags = requested_tags
                else:
                    tags = (tag,)
                for tag in tags:
                    seen.add((check, tag))
                    evidence = record
                    if evidence["outcome"] == "error" and not evidence.get("reason"):
                        message = _error_message(model.get("model_error"), issues.get(tag), run.data.get("infra_error"))
                        evidence = {**evidence, "reason": message or "Infrastructure error"}
                    _remember(cells, Cell(run, model, evidence, tag, installed.get(tag, {})))
            for check in selected:
                if check not in CHECK_IDS:
                    continue
                tags = (requested_tags or {""}) if check == "B6" else ("",)
                for tag in tags:
                    if (check, tag) in seen:
                        continue
                    message = _error_message(model.get("model_error"), issues.get(tag), run.data.get("infra_error"))
                    if message:
                        record = {"check_id": check, "outcome": "error", "reason": message, "attempts": []}
                        _remember(cells, Cell(run, model, record, tag, installed.get(tag, {})))
    return cells, models, harnesses


def _scope(path, root, repo_scope):
    path = Path(os.path.abspath(path.expanduser()))
    if path.is_relative_to(root):
        return safe_path(root, path), root
    if not path.is_relative_to(repo_scope) or repo_scope.resolve() != repo_scope:
        raise ValueError(f"output must stay in the canary home or {repo_scope}: {path}")
    return safe_path(repo_scope, path), repo_scope


def _write(path, scope, content):
    path = safe_path(scope, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8",
                            errors="backslashreplace", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.close()
            safe_path(scope, temporary).replace(safe_path(scope, path))
        finally:
            file.close()
            safe_path(scope, temporary).unlink(missing_ok=True)


def _slug(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "unknown"


def _owned_draft(path):
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as file:
            marker = repro.DRAFT_MARKER.encode("utf-8")
            return file.readline(len(marker) + 3).rstrip(b"\r\n") == marker
    except OSError:
        return False


def _drafts(cells, versions, directory, scope):
    links = {}
    claimed = set()
    for key, cell in sorted(cells.items()):
        if cell.outcome not in DRAFT_OUTCOMES:
            continue
        version, model, check, tag = key
        older = versions[versions.index(version) + 1:]
        last_pass = next((v for v in older if (prior := cells.get((v, model, check, tag)))
                          and prior.outcome == "pass"), None) if _runtime_cell(cell) else None
        stamp = cell.run.timestamp.astimezone(UTC).date().isoformat()
        slug = f"{stamp}_ollama-{_slug(version)}_{_slug(model)}_{check}"
        if tag and sum(other[:3] == key[:3] for other in cells) > 1:
            slug += "_" + _slug(tag)
        path = directory / f"{slug}.md"
        suffix = 2
        while path.name.casefold() in claimed or (os.path.lexists(path) and not _owned_draft(path)):
            path = directory / f"{slug}_{suffix}.md"
            suffix += 1
        claimed.add(path.name.casefold())
        _write(path, scope, repro.render(cell, last_pass))
        links[key] = path
    for path in directory.glob("*.md"):
        if path.name.casefold() not in claimed and _owned_draft(path) and safe_path(scope, path) == path:
            path.unlink(missing_ok=True)
    return links


def _class(outcome):
    return "na" if outcome == "n/a" else outcome


def _e(value):
    return html.escape(str(value))


def _runtime_cell(cell):
    return cell.record["check_id"] != "B6" or cell.record.get("attribution") == "runtime"


def _regressed(cell, older):
    return (cell is not None and older is not None and older.outcome == "pass"
            and cell.outcome in DRAFT_OUTCOMES and _runtime_cell(cell))


def _draft_link(draft, output):
    try:
        href = quote(Path(os.path.relpath(draft, output.parent)).as_posix())
    except ValueError:
        href = draft.as_uri()
    return f'<a href="{_e(href)}">Repro draft</a>'


def _cell_html(cell, older, draft, output):
    if cell is None:
        return '<td><span class="empty" aria-label="No recorded result">—</span></td>'
    outcome = cell.outcome
    content = f'<span class="outcome {_class(outcome)}">{outcome}</span>'
    attempts = cell.record.get("attempts") or []
    if attempts:
        dots = "".join(
            f'<span class="{_class(status)}" title="Attempt {_e(index)}: {_e(status)}">●</span>'
            for index, attempt in enumerate(attempts, 1)
            if (status := attempt.get("outcome")) in OUTCOMES
        )
        content += f'<span class="attempts">{dots}</span>'
    if _regressed(cell, older):
        content += '<span class="regression">Regression</span>'
    if cell.record["check_id"] == "B6" and cell.record.get("attribution") == "harness":
        content += '<span class="harness-failure">Harness failure</span>'
    if reason := cell.record.get("reason"):
        content += f"<small>{_e(reason)}</small>"
    if draft is not None:
        content += _draft_link(draft, output)
    return f"<td>{content}</td>"


def _model_details(model):
    gpu = model.get("gpu") or {}
    offload = gpu.get("offload")
    digest = str(model.get("digest") or "").removeprefix("sha256:")[:12]
    offload = f"{offload:.1%}" if isinstance(offload, (int, float)) else "—"
    return _e(f"Digest {digest or '—'} · Runner {gpu.get('runner') or '—'} · GPU {offload} · "
              f"Source {model.get('source') or '—'}")


def _header(run, harnesses, *, stable_timestamp=False):
    data = run.data
    host = data.get("host") or {}
    ram = host.get("ram_bytes")
    ram = f"{ram / 1024 ** 3:.1f} GiB" if isinstance(ram, (int, float)) else host.get("ram") or "Unavailable"
    tags = ", ".join(f"{tag} ({entry.get('commit') or 'commit unavailable'})"
                     for tag, entry in sorted(harnesses.items(), reverse=True)) or "No recorded harness"
    items = {
        "Host": host.get("macos") or host.get("platform") or "Unavailable",
        "Chip": host.get("chip") or host.get("machine") or "Unavailable",
        "RAM": ram,
        "Canary": data.get("canary_version") or __version__,
        "Hermes": tags,
        "Generated": (run.timestamp if stable_timestamp else datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Latest run isolation": (data.get("isolation") or {}).get("status") or "Unrecorded",
    }
    return '<dl class="meta">' + "".join(f"<div><dt>{label}</dt><dd>{_e(value)}</dd></div>"
                                         for label, value in items.items()) + "</dl>"


def _summary(versions, cells, drafts, output):
    """Answer first: what the newest tested release looks like."""
    current = {key: cell for key, cell in sorted(cells.items()) if key[0] == versions[0]}
    counts = {outcome: sum(cell.outcome == outcome for cell in current.values()) for outcome in OUTCOMES}
    broken = [key for key, cell in current.items() if cell.outcome in DRAFT_OUTCOMES]
    verdict = (f'<p class="verdict fail">{len(broken)} of {len(current)} checks did not pass</p>' if broken
               else '<p class="verdict pass">No failing checks</p>')
    chips = "".join(f'<li class="outcome {_class(outcome)}">{count} {outcome}</li>'
                    for outcome, count in counts.items() if count)
    findings = "".join(
        f'<li><span class="outcome {_class(current[key].outcome)}">{current[key].outcome}</span>'
        f'<span><b>{_e(key[1])}</b> · {_e(f"{key[2]} {key[3]}".strip())} {_e(NAMES[key[2]])}</span>'
        f'{_draft_link(drafts[key], output) if key in drafts else ""}</li>' for key in broken)
    return ('<section class="summary" aria-label="Newest tested release"><div class="headline"><div>'
            f'<p class="eyebrow">Newest tested release</p><p class="release">Ollama {_e(versions[0])}</p></div>'
            f'{verdict}</div><ul class="legend">{chips}</ul>'
            + (f'<ul class="findings">{findings}</ul>' if findings else "") + '</section>')


def _tested(version, names, columns, cells):
    """Leave out models without a result for this version, such as a back-test of one model."""
    return [name for name in names if any((version, name, *column) in cells for column in columns)]


def _matrix(versions, names, columns, cells, models, drafts, output):
    head = "".join(f'<th scope="col">{_e(f"{check} {tag}".strip())}<small>{_e(NAMES[check])}</small></th>'
                   for check, tag in columns)
    groups = []
    for index, version in enumerate(versions):
        older = versions[index + 1] if index + 1 < len(versions) else None
        rows, tested = [], _tested(version, names, columns, cells)
        for name in tested:
            label = "" if rows else f'<th scope="rowgroup" rowspan="{len(tested)}">{_e(version)}</th>'
            tiles = "".join(_cell_html(cells.get((version, name, check, tag)), cells.get((older, name, check, tag)),
                                       drafts.get((version, name, check, tag)), output) for check, tag in columns)
            rows.append(f'<tr>{label}<th scope="row">{_e(name)}<small class="details">'
                        f'{_model_details(models.get((version, name), {}))}</small></th>{tiles}</tr>')
        if rows:
            groups.append(f'<tbody>{"".join(rows)}</tbody>')
    return ('<div class="scroll" role="region" aria-label="Compatibility matrix" tabindex="0"><table><thead><tr>'
            f'<th scope="col">Ollama</th><th scope="col">Model</th>{head}</tr></thead>{"".join(groups)}</table></div>')


def _svg(run, versions, names, columns, cells):
    """Render the matrix as one dark card, for embedding where HTML is not allowed."""
    left = 126 + 8 * max(map(len, names), default=0)
    width = max(left + 60 * len(columns) + 20, 560)
    parts = []
    for index, (check, tag) in enumerate(columns):
        x = left + 60 * index + 24
        parts.append(f'<text x="{x}" y="76" text-anchor="middle" font-weight="700">{_e(check)}</text>')
        if tag:
            parts.append(f'<text x="{x}" y="90" text-anchor="middle" font-size="9" fill="#94a3b8">{_e(tag)}</text>')
    y = 104
    for index, version in enumerate(versions):
        older = versions[index + 1] if index + 1 < len(versions) else None
        tested = _tested(version, names, columns, cells)
        if not tested:
            continue
        parts.append(f'<path stroke="#1e293b" d="M24 {y}H{width - 24}"/>'
                     f'<text x="28" y="{y + 29}" font-size="13" font-weight="700">{_e(version)}</text>')
        y += 12
        for name in tested:
            parts.append(f'<text x="110" y="{y + 16}" fill="#cbd5e1">{_e(name)}</text>')
            for offset, (check, tag) in enumerate(columns):
                cell, x = cells.get((version, name, check, tag)), left + 60 * offset
                if cell is None:
                    parts.append(f'<rect x="{x}" y="{y}" width="48" height="24" rx="6" fill="#131c31"/>')
                    continue
                outline = ' stroke="#f8fafc" stroke-width="2"' if _regressed(cell, cells.get((older, name, check, tag))) else ""
                parts.append(f'<rect x="{x}" y="{y}" width="48" height="24" rx="6" fill="{SVG_COLORS[cell.outcome]}"{outline}/>'
                             f'<path class="i" transform="translate({x + 24} {y + 12})" d="{SVG_ICONS[cell.outcome]}"/>')
            y += 32
        y += 4
    x = 28
    for outcome in OUTCOMES:
        parts.append(f'<rect x="{x}" y="{y + 10}" width="14" height="14" rx="4" fill="{SVG_COLORS[outcome]}"/>'
                     f'<text x="{x + 20}" y="{y + 21}" font-size="11" fill="#94a3b8">{outcome}</text>')
        x += 38 + 7 * len(outcome)
    date = run.timestamp.astimezone(UTC).date().isoformat()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{y + 44}" viewBox="0 0 {width} {y + 44}" '
            f'role="img" font-family="{MONO}" font-size="12" fill="#f8fafc">'
            f'<title>Stack Canary compatibility matrix, {date}</title>'
            f'<rect x=".5" y=".5" width="{width - 1}" height="{y + 43}" rx="14" fill="#0b1120" stroke="#1e293b"/>'
            f'<g transform="translate(24 16) scale(.45)">{BIRD}</g>'
            '<text x="62" y="38" font-family="system-ui,sans-serif" font-size="15" font-weight="700">Stack Canary</text>'
            f'<text x="{width - 28}" y="38" text-anchor="end" font-size="11" fill="#94a3b8">Ollama compatibility · {date}</text>'
            '<style>.i{fill:none;stroke:#0b1120;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round}</style>'
            + "".join(parts) + '</svg>\n')


EMOJI = {"pass": "✅", "fail": "❌", "flaky": "🟡", "timeout": "⏱️", "error": "⚠️", "n/a": "➖"}


def _markdown(run, versions, names, columns, cells, drafts, output):
    def row(parts):
        return "| " + " | ".join(str(part).replace("|", "\\|").replace("\n", " ") for part in parts) + " |"

    tags = ", ".join(tag for check, tag in columns if check == "B6" and tag) or "none recorded"
    isolation = (run.data.get("isolation") or {}).get("status", "unrecorded")
    lines = ["# Stack Canary compatibility matrix", "",
             f"- Date: {run.timestamp.astimezone(UTC).date().isoformat()} (UTC)",
             f"- Hermes: {tags}", f"- Isolation: {isolation}", ""]
    headings = ["Ollama", *(f"{name} {check} {tag}".strip() for name in names for check, tag in columns)]
    lines += [row(headings), row(["---"] * len(headings))]
    for index, version in enumerate(versions):
        older = versions[index + 1] if index + 1 < len(versions) else None
        values = [version]
        for name in names:
            for check, tag in columns:
                cell = cells.get((version, name, check, tag))
                prior = cells.get((older, name, check, tag))
                marker = EMOJI[cell.outcome] if cell else "—"
                if _regressed(cell, prior):
                    marker += " 🔻"
                values.append(marker)
        lines.append(row(values))
    lines += ["", "## Reproduction drafts", ""]
    lines.extend(f"- [{path.name}]({quote(Path(os.path.relpath(path, output.parent)).as_posix())})"
                 for path in sorted(drafts.values()))
    if not drafts:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def generate(directories=None, output=Path("report/index.html"), repro_dir=Path("repro"), *,
             all_models=False, stable_timestamp=False):
    root = canary_home()
    cwd = Path.cwd().resolve()
    output, report_scope = _scope(output, root, cwd / "report")
    directory, repro_scope = _scope(repro_dir, root, cwd / "repro")
    runs = load_runs(directories or [root / "results", cwd / "results"])
    if not runs:
        raise ValueError("no Stack Canary results found")
    cells, models, harnesses = merge(runs)
    versions = sorted({run.version for run in runs}, key=_version_order, reverse=True)
    tags = {tag for _, _, check, tag in cells if check == "B6"} or {""}
    if all_models:
        names = sorted({name for _, name in models})
    else:
        # Watch reruns only missing models, so the newest run may hold a subset of the configured set.
        recorded = {name for _, name in models}
        names = sorted(name for name in config.load().models if name in recorded)
        if not names:
            latest = next((run for run in reversed(runs) if isinstance(run.data.get("runtime"), dict)), runs[-1])
            names = sorted({model["name"] for model in latest.data["models"]
                            if isinstance(model, dict) and isinstance(model.get("name"), str)})
        visible = set(names)
        cells = {key: cell for key, cell in cells.items() if key[1] in visible}
    columns = [(check, "") for check in CHECKS] + [
        ("B6", tag) for tag in sorted(tags, key=_version_order, reverse=True)
    ]
    drafts = _drafts(cells, versions, directory, repro_scope)
    legend = '<ul class="legend" aria-label="Outcome legend">' + "".join(
        f'<li class="outcome {_class(outcome)}">{outcome}</li>' for outcome in OUTCOMES
    ) + '</ul>'
    title = "Stack Canary compatibility report"
    lede = ("The same small protocol checks, replayed against every Ollama release for each model. "
            "A red cell means an agent built on that combination breaks without an error.")
    page = ('<!doctype html>\n<html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title}</title><meta name="description" content="{lede}">'
            f'<meta property="og:title" content="{title}"><meta property="og:description" content="{lede}">'
            f'<meta property="og:image" content="{REPO.replace("github.com", "raw.githubusercontent.com")}'
            '/main/assets/social-preview.png"><meta name="twitter:card" content="summary_large_image">'
            f'<link rel="icon" href="data:image/svg+xml,{quote(LOGO)}"><style>{STYLE}</style>'
            f'<header><a class="brand" href="{REPO}">{LOGO}Stack Canary</a>'
            f'<a class="button" href="{REPO}">GitHub</a></header>'
            '<main><p class="eyebrow">Compatibility report</p><h1>Tool-call compatibility across Ollama releases</h1>'
            f'<p class="lede">{lede}</p>{_summary(versions, cells, drafts, output)}'
            f'<h2>Matrix</h2>{_matrix(versions, names, columns, cells, models, drafts, output)}{legend}'
            f'<h2>Test bench</h2>{_header(runs[-1], harnesses, stable_timestamp=stable_timestamp)}'
            '<h2>Run it yourself</h2><pre>git clone ' + REPO + ' &amp;&amp; cd stack-canary\n'
            'python -m pip install -e .\n'
            'canary run --ollama last:2 --models llama3.2:3b --context-length 8192  # managed runs need macOS\n'
            'canary report</pre></main>'
            f'<footer><span>Generated by Stack Canary {_e(__version__)}. It measures protocol behavior, not answer quality.</span>'
            f'<a href="{REPO}#checks">What the checks send</a></footer></html>\n')
    _write(output, report_scope, page)
    if output.name == "index.html":
        markdown = output.with_name("MATRIX.md")
        _write(markdown, report_scope, _markdown(runs[-1], versions, names, columns, cells, drafts, markdown))
        _write(output.with_name("matrix.svg"), report_scope, _svg(runs[-1], versions, names, columns, cells))
    return output, len(drafts)
