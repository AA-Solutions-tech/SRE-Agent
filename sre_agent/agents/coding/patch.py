"""Patch-based fixing for the coding agent (pure Python; the LLM is injected as `ask`).

The model never rewrites a whole file. For each fix site it returns small edits
{file, search, replace}; code applies an edit only if `search` matches the file EXACTLY ONCE.

  files + investigation -> tasks (one per code region) -> LLM asks run in PARALLEL
  -> edits grouped per file -> applied one by one in code -> diff-size check
  -> write -> syntax check (reverted on failure) -> a failed edit is retried with its error.
"""
import concurrent.futures as cf
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

from sre_agent.core.git_tools import safe_path

from .prompts import SYSTEM_PATCH, patch_prompt

SMALL_FILE_CHARS = 60_000     # input size is cheap, output is not: files up to this go to the model whole
WINDOW_RADIUS = 70            # lines either side of a cited line for bigger files
MAX_REMOVED_LINES = 200       # a patch deleting more than this is rejected
MAX_REMOVED_RATIO = 0.3       # ...or more than this share of the file
MAX_WORKERS = 4
SYNTAX_TIMEOUT = 30
ENC = "surrogateescape"       # round-trips bytes that are not valid UTF-8 unchanged
NL = chr(10)


# ---------------- parsing and applying edits ----------------
def parse_edits(text: str) -> list[dict]:
    """Edits from the model's JSON reply. Raises ValueError if there is no JSON object."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in model reply")
    obj = json.loads(m.group(0))
    out = []
    for e in obj.get("edits") or []:
        if isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("file", "search", "replace")):
            out.append({"file": e["file"].strip(), "search": e["search"], "replace": e["replace"],
                        "reason": str(e.get("reason", ""))[:200]})
    return out


def _locate(text: str, search: str) -> tuple[str, tuple[int, int] | None]:
    """('ok', (start, end)) | ('missing', None) | ('ambiguous', None). Tolerates trailing-space drift."""
    idx = text.find(search)
    if idx != -1:
        return ("ambiguous", None) if text.find(search, idx + 1) != -1 else ("ok", (idx, idx + len(search)))
    want = [ln.rstrip() for ln in search.splitlines()]
    while want and not want[-1]:
        want.pop()
    if not want:
        return "missing", None
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    hits = [i for i in range(len(lines) - len(want) + 1)
            if all(lines[i + k].rstrip() == want[k] for k in range(len(want)))]
    if not hits:
        return "missing", None
    if len(hits) > 1:
        return "ambiguous", None
    i = hits[0]
    return "ok", (offsets[i], offsets[i + len(want) - 1] + len(lines[i + len(want) - 1].rstrip("\r\n")))


def _locate_indent_free(text: str, search: str) -> tuple[str, tuple[int, int] | None, str, str]:
    """Last-resort match ignoring indentation: the same consecutive lines once leading/trailing blanks are stripped.
    Returns (status, whole-line span, file indent, search indent)."""
    want = [ln.strip() for ln in search.splitlines()]
    while want and not want[-1]:
        want.pop()
    while want and not want[0]:
        want.pop(0)
    if not want:
        return "missing", None, "", ""
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    hits = [i for i in range(len(lines) - len(want) + 1)
            if all(lines[i + k].strip() == want[k] for k in range(len(want)))]
    if len(hits) != 1:
        return ("ambiguous" if hits else "missing"), None, "", ""
    i = hits[0]
    first_search = next(ln for ln in search.splitlines() if ln.strip())
    file_lead = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
    search_lead = first_search[:len(first_search) - len(first_search.lstrip())]
    end = offsets[i + len(want) - 1] + len(lines[i + len(want) - 1].rstrip("\r\n"))
    return "ok", (offsets[i], end), file_lead, search_lead


def _shift_indent(replace: str, search_lead: str, file_lead: str, nl: str) -> str:
    """Re-base the replacement from the model's indentation to the file's, keeping relative indentation."""
    out = []
    for ln in replace.split(nl):
        if not ln.strip():
            out.append("")
            continue
        out.append(file_lead + (ln[len(search_lead):] if ln.startswith(search_lead) else ln.lstrip()))
    return nl.join(out)


def _closest_hint(text: str, search: str) -> str:
    """For a search that matched nothing: name the nearest real line so the retry can copy it exactly."""
    want = [ln.strip() for ln in search.splitlines() if ln.strip()]
    first = want[0] if want else ""
    lines = text.splitlines()
    stripped = [ln.strip() for ln in lines]
    best, best_at = 0, -1  # where does the search agree with the file for the most lines?
    for at, ln in enumerate(stripped):
        if ln != first:
            continue
        k = 0
        while k < len(want) and at + k < len(stripped) and stripped[at + k] == want[k]:
            k += 1
        if k > best:
            best, best_at = k, at
    if best_at >= 0 and best < len(want):
        return (f". Your search matches the file at line {best_at + 1} for {best} line(s), then differs: "
                f"you wrote {want[best][:100]!r} but line {best_at + best + 1} is "
                f"{stripped[best_at + best][:100] if best_at + best < len(stripped) else '(end of file)'!r}")
    near = difflib.get_close_matches(first, stripped, n=1, cutoff=0.6) if first else []
    if not near:
        return f". You searched for: {first[:100]!r}, which is not in the code"
    at = next(i for i, ln in enumerate(lines, 1) if ln.strip() == near[0])
    return f". You searched for {first[:100]!r}; the closest real line is {at}: {near[0][:120]!r}"


def _reindent(text: str, start: int, search: str, replace: str, nl: str) -> str:
    """When the model's search began mid-line (it dropped the indent), its later replace lines are relative
    to column 0: prefix them with the indent that was left in the file before the match."""
    line_start = text.rfind("\n", 0, start) + 1
    lead = text[line_start:start]
    if not lead or lead.strip() or search[:1].isspace():
        return replace
    first, *rest = replace.split(nl)
    return nl.join([first] + [lead + ln if ln.strip() else ln for ln in rest])


def apply_edits(content: str, edits: list[dict]) -> tuple[str, list[str | None]]:
    """Apply edits in order. Returns (new_content, errors) with errors[i] = None when edit i applied."""
    nl = "\r\n" if "\r\n" in content else "\n"
    text, errors = content, []
    for e in edits:
        search = e["search"].replace("\r\n", "\n").replace("\n", nl)
        replace = e["replace"].replace("\r\n", "\n").replace("\n", nl)
        if not search.strip():
            errors.append("empty search text")
            continue
        status, span = _locate(text, search)
        if status == "missing":
            status3, span3, file_lead, search_lead = _locate_indent_free(text, search)
            if status3 == "ok":  # same lines, different indentation: patch there, in the file's indentation
                text = text[:span3[0]] + _shift_indent(replace, search_lead, file_lead, nl) + text[span3[1]:]
                errors.append(None)
                continue
            if status3 == "ambiguous":
                errors.append("search text matches more than one place (include more surrounding lines)")
                continue
            errors.append("search text not found (copy it exactly from the code shown, without the "
                          "line-number prefix)" + _closest_hint(text, search))
        elif status == "ambiguous":
            errors.append("search text matches more than one place (include more surrounding lines)")
        else:
            text = text[:span[0]] + _reindent(text, span[0], search, replace, nl) + text[span[1]:]
            errors.append(None)
    return text, errors


def check_diff(original: str, new: str) -> tuple[bool, int, int]:
    """(ok, removed_lines, added_lines): rejects patches that delete too much of the file."""
    removed = added = 0
    for ln in difflib.unified_diff(original.splitlines(), new.splitlines(), lineterm="", n=0):
        if ln.startswith("-") and not ln.startswith("---"):
            removed += 1
        elif ln.startswith("+") and not ln.startswith("+++"):
            added += 1
    total = max(1, len(original.splitlines()))
    ok = removed <= MAX_REMOVED_LINES and (total < 20 or removed <= MAX_REMOVED_RATIO * total)
    return ok, removed, added


def syntax_check(path: Path, cwd: Path) -> dict | None:
    """Check a changed file parses. None when no checker applies (or node is not installed)."""
    suffix = path.suffix.lower()
    rel = path.relative_to(cwd.resolve()).as_posix() if path.is_absolute() else str(path)
    if suffix in (".js", ".mjs", ".cjs"):
        cmd = ["node", "--check", str(path)]
    elif suffix == ".py":
        cmd = [sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())", str(path)]
    elif suffix == ".json":
        try:
            json.loads(path.read_bytes().decode("utf-8", ENC))
            return {"command": f"json parse {rel}", "exit_code": 0, "passed": True, "output_tail": ""}
        except ValueError as e:
            return {"command": f"json parse {rel}", "exit_code": 1, "passed": False, "output_tail": str(e)[-500:]}
    else:
        return None
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=SYNTAX_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    label = f"node --check {rel}" if suffix != ".py" else f"python syntax check {rel}"
    return {"command": label, "exit_code": r.returncode, "passed": r.returncode == 0,
            "output_tail": (r.stdout + r.stderr)[-500:]}


# ---------------- tasks (one per code region) ----------------
def _merge_windows(n_lines: int, ref_lines: list[int]) -> list[tuple[int, int]]:
    spans = sorted((max(1, ln - WINDOW_RADIUS), min(n_lines, ln + WINDOW_RADIUS)) for ln in ref_lines)
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def build_tasks(files: dict[str, str], inv: dict, notes: list[str]) -> list[dict]:
    """Small files -> one whole-file task; big files -> one task per window around the cited lines."""
    refs = [r for r in (inv.get("code_refs") or []) + (inv.get("verified_citations") or []) if isinstance(r, dict)]
    tasks = []
    for path, text in files.items():
        n = len(text.splitlines())
        lines_for = [r["line"] for r in refs if r.get("path") == path and isinstance(r.get("line"), int)]
        if len(text) <= SMALL_FILE_CHARS:
            spans = [(1, n)]
        elif lines_for:
            spans = _merge_windows(n, lines_for)
        else:
            notes.append(f"{path}: large file with no cited line - skipped (cannot choose a region safely)")
            continue
        tasks += [{"file": path, "start": a, "end": b, "errors": []} for a, b in spans]
    return tasks


def _ask_task(task: dict, text: str, inv: dict, ask) -> list[dict]:
    lines = text.splitlines()
    body = NL.join(f"{i:>5}| {lines[i - 1]}" for i in range(task["start"], min(task["end"], len(lines)) + 1))
    prompt = patch_prompt(task["file"], task["start"], task["end"], len(lines), body, inv, task["errors"],
                          sweep=task.get("sweep", False))
    edits = parse_edits(ask(SYSTEM_PATCH + NL + NL + prompt))
    for e in edits:
        e["file"] = task["file"]  # one task = one file; ignore a wrong path from the model
    return edits


def _run_round(tasks: list[dict], current: dict[str, str], inv: dict, ask) -> tuple[list[dict], list[dict]]:
    """Run all tasks in parallel, apply their edits in code. Returns (applied edits, tasks to retry)."""
    grouped: dict[str, list[tuple[dict, dict]]] = {}
    retry: list[dict] = []
    done: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(tasks)))) as pool:
        futures = {pool.submit(_ask_task, t, current[t["file"]], inv, ask): t for t in tasks}
        for fut in cf.as_completed(futures):
            t = futures[fut]
            try:
                for e in fut.result():
                    grouped.setdefault(t["file"], []).append((t, e))
            except Exception as exc:  # LLM/parse failure: retry this region once
                retry.append({**t, "errors": [f"previous attempt failed: {type(exc).__name__}: {exc}"[:200]]})
    for path, items in grouped.items():
        base = current[path]
        items.sort(key=lambda it: base.find(it[1]["search"]) if it[1]["search"] in base else len(base))
        new_text, errors = apply_edits(base, [e for _, e in items])
        current[path] = new_text
        for (t, e), err in zip(items, errors):
            if err is None:
                done.append(e)
            elif not t.get("sweep"):  # a failed sweep edit is just dropped
                retry.append({**t, "errors": [f"edit '{e['reason'] or e['search'][:40]}': {err}"]})
    merged: dict[tuple, dict] = {}
    for t in retry:  # one retry task per region, with all its errors
        key = (t["file"], t["start"], t["end"])
        if key in merged:
            merged[key]["errors"] += t["errors"]
        else:
            merged[key] = t
    return done, list(merged.values())


def fix_files(wt: Path, files: dict[str, str], inv: dict, ask, rounds: int = 2, sweep: bool = True) -> dict:
    """Patch `files` (path -> text) inside worktree `wt`. Returns a summary dict (see keys below)."""
    original, current = dict(files), dict(files)
    notes: list[str] = []
    tasks = build_tasks(current, inv, notes)
    applied: list[dict] = []
    for rnd in range(1, max(1, rounds) + 1):
        if not tasks:
            break
        done, tasks = _run_round(tasks, current, inv, ask)
        applied += done
        if tasks and rnd == max(1, rounds):
            notes += [f"{t['file']}:{t['start']}-{t['end']}: gave up: {'; '.join(t['errors'])}" for t in tasks]
    if sweep and applied:  # one more look for the SAME problem in other places of the shown code
        already = [f"{e['reason'] or e['search'][:50]}" for e in applied]
        sweep_tasks = [{**t, "sweep": True, "errors": already}
                       for t in build_tasks(current, inv, [])]
        done, _ = _run_round(sweep_tasks, current, inv, ask)
        applied += done

    changed, checks, failures = [], [], list(notes)
    for path, new_text in current.items():
        if new_text == original[path]:
            continue
        ok, removed, added = check_diff(original[path], new_text)
        if not ok:
            failures.append(f"{path}: patch rejected, it removes {removed} lines (limit {MAX_REMOVED_LINES})")
            continue
        target = safe_path(wt, path)
        target.write_bytes(new_text.encode("utf-8", ENC))
        result = syntax_check(target, wt)
        if result is not None:
            checks.append(result)
            if not result["passed"]:
                target.write_bytes(original[path].encode("utf-8", ENC))  # revert: never leave broken code
                failures.append(f"{path}: reverted, syntax check failed: {result['output_tail'][:200]}")
                continue
        changed.append(path)
    return {"changed": changed, "checks": checks, "failures": failures, "edits_applied": len(applied)}
