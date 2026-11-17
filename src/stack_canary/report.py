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

from . import __version__, repro
from .checks import CHECKS
from .releases import version_key
from .runtime_ollama import canary_home, safe_path

OUTCOMES = ("pass", "fail", "flaky", "timeout", "error", "n/a")
DRAFT_OUTCOMES = {"fail", "flaky", "timeout"}
CHECK_IDS = (*CHECKS, "B6")
STYLE = """
:root {
  color-scheme: light dark;
  --bg:#f6f7f9; --panel:#fff; --text:#182333; --muted:#46566a; --line:#c7d0db; --link:#17549a;
  --pass:#117848; --fail:#b8293d; --flaky:#925500; --timeout:#7638a6; --error:#a33a13; --na:#596879;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#141a24; --panel:#1d2634; --text:#edf2fa; --muted:#b6c6d9; --line:#465468; --link:#a5d0ff;
          --pass:#82dda7; --fail:#ff9ea8; --flaky:#f9c16d; --timeout:#d8adff; --error:#ffb394; --na:#bfccd9; }
}
* { box-sizing:border-box }
body { margin:0; padding:2rem clamp(1rem, 4vw, 4rem); font:15px/1.5 system-ui,sans-serif; background:var(--bg); color:var(--text) }
h1 { margin:0 0 .75rem; font-size:2rem; letter-spacing:-.025em }
summary { cursor:pointer }
.meta { display:flex; flex-wrap:wrap; gap:.5rem 1.5rem; color:var(--muted); margin:0 0 1rem }
.meta div { display:flex; gap:.45rem }
.meta dt,.meta dd { margin:0 }
.meta dt { font-weight:650; color:var(--text) }
.scroll { overflow:auto; border:1px solid var(--line); border-radius:.6rem; background:var(--panel) }
table { border-collapse:collapse; width:100%; text-align:left }
th,td { padding:.55rem .65rem; border-bottom:1px solid var(--line); white-space:nowrap }
tbody:last-child tr:last-child td,tbody:last-child th[scope=rowgroup] { border-bottom:0 }
.group { border-left:1px solid var(--line) }
.details { color:var(--muted); font-size:.83rem }
td span { display:block }
.cell small { display:block; color:var(--muted); white-space:normal; min-width:7rem }
a { color:var(--link) }
.outcome { font-weight:700 }
.pass { color:var(--pass) } .fail { color:var(--fail) } .flaky { color:var(--flaky) }
.timeout { color:var(--timeout) } .error { color:var(--error) } .na,.empty { color:var(--na) }
.attempts { margin-top:.15rem; letter-spacing:.14em; line-height:1.15 }
.attempts span { display:inline }
.regression { color:var(--fail); font-weight:700; font-size:.78rem }
.legend { display:flex; flex-wrap:wrap; gap:.8rem; list-style:none; padding:0 }
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


def _harness_records(run):
    info = run.data.get("harness") or {}
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


def merge(runs):
    cells, models, harnesses = {}, {}, {}
    for run in runs:
        selected = (run.data.get("settings") or {}).get("checks") or CHECK_IDS
        installed = _harness_records(run)
        harnesses.update(installed)
        issues = {issue.get("tag", ""): issue for issue in run.data.get("harness_errors") or []}
        requested_tags = installed.keys() | issues.keys() | set(run.data.get("hermes_tags") or [])
        for model in run.data["models"]:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                continue
            key = (run.version, model["name"])
            records = model.get("checks") or []
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


def _drafts(cells, versions, directory, scope):
    links = {}
    claimed = set()
    for key, cell in sorted(cells.items()):
        if cell.outcome not in DRAFT_OUTCOMES:
            continue
        version, model, check, tag = key
        older = versions[versions.index(version) + 1:]
        last_pass = next((v for v in older if (prior := cells.get((v, model, check, tag)))
                          and prior.outcome == "pass"), None)
        stamp = cell.run.timestamp.astimezone(UTC).date().isoformat()
        slug = f"{stamp}_ollama-{_slug(version)}_{_slug(model)}_{check}"
        if tag and sum(other[:3] == key[:3] for other in cells) > 1:
            slug += "_" + _slug(tag)
        path = directory / f"{slug}.md"
        suffix = 2
        while path.name.casefold() in claimed:
            path = directory / f"{slug}_{suffix}.md"
            suffix += 1
        claimed.add(path.name.casefold())
        _write(path, scope, repro.render(cell, last_pass))
        links[key] = path
    return links


def _class(outcome):
    return "na" if outcome == "n/a" else outcome


def _e(value):
    return html.escape(str(value))


def _cell_html(cell, older, draft, output, *, group):
    group_attr = ' class="group"' if group else ""
    if cell is None:
        return f'<td{group_attr}><span class="empty" aria-label="No recorded result">—</span></td>'
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
    if older is not None and older.outcome == "pass" and outcome != "pass":
        content += '<span class="regression">Regression</span>'
    if reason := cell.record.get("reason"):
        content += f"<small>{_e(reason)}</small>"
    if draft is not None:
        try:
            href = quote(Path(os.path.relpath(draft, output.parent)).as_posix())
        except ValueError:
            href = draft.as_uri()
        content += f'<a href="{_e(href)}">Repro draft</a>'
    return f"<td{group_attr}>{content}</td>"


def _model_details(model):
    gpu = model.get("gpu") or {}
    offload = gpu.get("offload")
    digest = str(model.get("digest") or "").removeprefix("sha256:")[:12]
    offload = f"{offload:.1%}" if isinstance(offload, (int, float)) else "—"
    return _e(f"Digest {digest or '—'} · Runner {gpu.get('runner') or '—'} · GPU {offload} · "
              f"Source {model.get('source') or '—'}")


def _header(run, harnesses):
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
        "Generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "Latest run isolation": (data.get("isolation") or {}).get("status") or "Unrecorded",
    }
    return '<dl class="meta">' + "".join(f"<div><dt>{label}</dt><dd>{_e(value)}</dd></div>"
                                         for label, value in items.items()) + "</dl>"


def _matrix(versions, names, columns, cells, models, drafts, output):
    count = len(columns)
    title = '<tr><th rowspan="2" scope="col">Ollama</th>' + "".join(
        f'<th class="group" scope="colgroup" colspan="{count}">{_e(name)}</th>' for name in names
    ) + "</tr>"
    labels = "".join(
        f'<th scope="col"{" class=group" if index == 0 else ""}>{_e(check + (" " + tag if tag else ""))}</th>'
        for _ in names for index, (check, tag) in enumerate(columns)
    )
    rows = []
    for index, version in enumerate(versions):
        older = versions[index + 1] if index + 1 < len(versions) else None
        details = "".join(f'<td class="details group" colspan="{count}">{_model_details(models.get((version, name), {}))}</td>'
                          for name in names)
        rows.append(f'<tbody><tr><th scope="rowgroup" rowspan="2">{_e(version)}</th>{details}</tr><tr>')
        for name in names:
            for offset, (check, tag) in enumerate(columns):
                key = (version, name, check, tag)
                rows.append(_cell_html(cells.get(key), cells.get((older, name, check, tag)),
                                       drafts.get(key), output, group=offset == 0))
        rows.append("</tr></tbody>")
    return f'<div class="scroll"><table><thead>{title}<tr>{labels}</tr></thead>{"".join(rows)}</table></div>'


def generate(directories=None, output=Path("report/index.html"), repro_dir=Path("repro")):
    root = canary_home()
    cwd = Path.cwd().resolve()
    output, report_scope = _scope(output, root, cwd / "report")
    directory, repro_scope = _scope(repro_dir, root, cwd / "repro")
    runs = load_runs(directories or [root / "results", cwd / "results"])
    if not runs:
        raise ValueError("no Stack Canary results found")
    cells, models, harnesses = merge(runs)
    versions = sorted({run.version for run in runs}, key=_version_order, reverse=True)
    names = sorted({name for _, name in models})
    tags = {tag for _, _, check, tag in cells if check == "B6"} or {""}
    columns = [(check, "") for check in CHECKS] + [
        ("B6", tag) for tag in sorted(tags, key=_version_order, reverse=True)
    ]
    drafts = _drafts(cells, versions, directory, repro_scope)
    legend = '<ul class="legend" aria-label="Outcome legend">' + "".join(
        f'<li class="outcome {_class(outcome)}">{outcome}</li>' for outcome in OUTCOMES
    ) + '</ul>'
    matrix = _matrix(versions, names, columns, cells, models, drafts, output)
    page = ('<!doctype html>\n<html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Stack Canary compatibility report</title><style>{STYLE}</style>'
            f'<main><h1>Stack Canary compatibility report</h1>{_header(runs[-1], harnesses)}{matrix}{legend}</main></html>\n')
    _write(output, report_scope, page)
    return output, len(drafts)
