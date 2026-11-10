"""Deterministic diagnosis for operational errors (config, credentials, rate limit, data, server, external,
missed job). No LLM: the category came from sre.classify, the facts come from runtime checks, and the card
says plainly what was verified and what was not.

    diagnose(signal, related, classification) -> analysis dict (same shape as code_inspect, plus notice=True)
    build_notice_card(...)                    -> (title, body) "action needed" card, no Approve/Reject
"""
import re

from sre_agent.sre import classify, issues, runtime_checks

from .rca import pretty_line

NL = chr(10)
FILE_HITS = 3
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

IMPACT = {
    "config": "Anything that needs this setting fails until it is provided; the app keeps running without it.",
    "credentials": "Calls that use this credential are refused, so the feature behind it is down.",
    "rate_limit": "Requests are being throttled; affected calls fail or slow down while the limit lasts.",
    "data": "Only the records named below are affected; the job or service itself is working.",
    "server": "The host or a dependency it needs is unreachable or out of resources; dependent features fail.",
    "external": "A service this app depends on is failing; features that call it are degraded.",
    "job_missed": "Whatever this job produces (emails, reports, syncs) did not happen for this run.",
    "tls": "Clients may refuse to connect, or this app cannot talk to a service whose certificate is bad.",
    "security": "Possible abuse of login or request paths; no outage yet, but an account or data may be at risk.",
    "database": "Anything that reads or writes the database fails or slows down; this is usually wide.",
    "deploy": "The code cannot run correctly in this environment, so the affected feature is broken from this deploy.",
    "queue": "Background work is not being processed, so emails, syncs or reports fall behind.",
}


def _code_refs(term: str) -> list[dict]:
    """Where `term` appears in the repository (up to FILE_HITS source files). Empty when there is no repo."""
    from . import code_inspect, investigate as inv

    repo = code_inspect._repo()
    if repo is None or not term:
        return []
    try:
        rows = inv._hits(inv._git(repo, "grep", "-n", "-I", "-F", "-e", term, check=False),
                         lambda p: not inv._skip(p), FILE_HITS)
    except Exception:  # noqa: BLE001 - a missing code reference never blocks the notice
        return []
    refs = []
    for row in rows:
        path, line, _ = row.split(":", 2)
        refs.append({"path": path, "line": int(line)})
    return refs


def _config(signal, d, related):
    var = d.get("variable")
    checks, refs = [], []
    if not var:
        return dict(confidence=0.5, refs=refs, checks=checks,
                    root_cause="The app reports a missing key or setting, but the log line does not say which one.",
                    remediation="Find the setting named in the error in the app's configuration and provide it.",
                    next_steps=["Open the log lines below and note the name of the missing setting.",
                                "Set APP_ENV_FILES so the agent can check keys by name next time."])
    status = runtime_checks.env_status(var)
    for s in status["sources"]:
        checks.append(f"`{var}` in `{s['source']}`: {s['state']}")
    if status["similar"]:
        checks.append("Similar names found there: " + ", ".join(f"`{n}`" for n in status["similar"])
                      + " (a typo in the key name?)")
    refs = _code_refs(var)
    bad = [s for s in status["sources"] if s["state"] in ("missing", "empty", "placeholder")]
    if not status["checked"]:
        checks.append("Not verified: the agent has no env file to look at. Set APP_ENV_FILES "
                      "(names and set/empty status only, values are never read out).")
        cause, conf = (f"The app reports `{var}` is missing or empty. This comes from the log line only; the "
                       "server's environment was not checked."), 0.7
    elif bad:
        where = ", ".join(f"`{s['source']}` ({s['state']})" for s in bad)
        cause, conf = f"`{var}` is not usable: {where}. That is what the app is complaining about.", 0.9
    else:
        cause, conf = (f"`{var}` is set in the env file, yet the app reports it missing. The running process "
                       "probably started before the value was added, or it loads a different env file."), 0.6
    return dict(confidence=conf, refs=refs, checks=checks, root_cause=cause,
                remediation=f"Set a real value for `{var}` in the app's environment (.env or secret manager), "
                            "then restart the app so it is picked up. No code change is needed.",
                next_steps=[f"Add or correct `{var}` where the app loads its environment.",
                            "Restart the app (or reload its config) and watch the log for the error to stop.",
                            "Keep real values out of git; update .env.example if the key is new."])


def _credentials(signal, d, related):
    return dict(confidence=0.8, refs=[], checks=[f"Matched in the log: `{d.get('matched', '')}`"],
                root_cause=(f"The service rejected the credentials this app presented (`{d.get('matched', '')}`). "
                            "The key, token or password is wrong, expired or revoked. Editing code cannot fix that."),
                remediation="Issue a fresh key/token (or correct the password) at the provider, update it in the "
                            "app's environment, and restart.",
                next_steps=["Find which credential the failing call uses and check it at the provider.",
                            "Rotate it if it may have leaked, then update the env file / secret manager.",
                            "Restart the app and confirm the error stops."])


def _rate_limit(signal, d, related):
    n, w = d.get("lines", 1), issues.watch_window().total_seconds() / 60
    return dict(confidence=0.8, refs=[], checks=[f"Matched in the log: `{d.get('matched', '')}`",
                                                  f"Persisted past the alert threshold ({issues.watch_threshold()} hits within {w:g} min)"],
                root_cause=(f"The upstream API or service is throttling this app (`{d.get('matched', '')}`), "
                            f"{n} line(s) in this batch. A short burst would have been ignored; this kept going."),
                remediation="Lower the call rate or batch requests, add retry with backoff, or raise the plan/quota "
                            "at the provider. Backoff would be a code change you can ask for separately.",
                next_steps=["Check the provider's usage dashboard for the quota that was hit.",
                            "Find what is calling so often (a loop, a retry storm, a new feature).",
                            "Raise the quota or add backoff."])


def _data(signal, d, related):
    emails, n = d.get("emails") or [], d.get("lines", 1)
    who = ", ".join(f"`{e}`" for e in emails) or "the recipient in the log line"
    return dict(confidence=0.8, refs=[], checks=[f"{n} failure line(s) in this batch, {len(emails)} distinct address(es)",
                                                  f"Matched in the log: `{d.get('matched', '')}`"],
                root_cause=(f"The mail server rejected {who} (`{d.get('matched', '')}`). The job itself ran and "
                            "the mail system works; this address is invalid or unreachable."
                            + (" Other recipients were not necessarily affected." if len(emails) <= 1 else "")),
                remediation=f"Correct or remove {who} in your data (employee/contact record). No code change needed.",
                next_steps=["Fix the address at its source, or mark the contact inactive.",
                            "If every recipient is failing, this is not a bad address: check SMTP settings instead.",
                            "Optional follow-up: make the job log and skip a bad address instead of failing."])


def _server(signal, d, related):
    checks = []
    down = []
    for r in runtime_checks.tcp_checks(d.get("hosts") or []):
        label = f"`{r['host']}:{r['port']}`"
        if r["reachable"] is True:
            checks.append(f"{label} accepts connections right now")
        elif r["reachable"] is False:
            checks.append(f"{label} is NOT reachable ({r['error']})")
            down.append(label)
        else:
            checks.append(f"{label}: {r['error']}")
    cause = (f"{', '.join(down)} is not accepting connections, so the app cannot reach it." if down else
             f"The app hit an infrastructure error (`{d.get('matched', '')}`): a connection, disk, memory or "
             "file-handle limit.")
    return dict(confidence=0.85 if down else 0.6, refs=[], checks=checks or [f"Matched in the log: `{d.get('matched', '')}`"],
                root_cause=cause,
                remediation="Check that the service/host is running and healthy (process, disk space, memory, "
                            "open-file limit), restart or free resources, then watch the log.",
                next_steps=["Log in to the server and check the service status, disk (`df -h`) and memory.",
                            "Look for a recent deploy, restart or crash of the dependency.",
                            "Confirm the error stops after the fix."])


def _external(signal, d, related):
    n, w = d.get("lines", 1), issues.watch_window().total_seconds() / 60
    return dict(confidence=0.7, refs=[], checks=[f"Matched in the log: `{d.get('matched', '')}`",
                                                  f"Persisted past the alert threshold ({issues.watch_threshold()} hits within {w:g} min)"],
                root_cause=(f"A service this app calls is failing or unreachable (`{d.get('matched', '')}`), "
                            f"{n} line(s) in this batch. It kept happening rather than being a single blip."),
                remediation="Check the provider's status page and the endpoint the app calls; if it is your own "
                            "service, check it is up. Usually nothing to change here until it recovers.",
                next_steps=["Check the provider's status page.",
                            "Test the failing endpoint from the server.",
                            "Decide if the app needs a fallback or retry for this dependency."])


def _job_unfinished(signal, d, related):
    return dict(confidence=0.7, refs=[],
                checks=[f"Job `{d.get('job')}` started at {str(d.get('started_at', ''))[:16]}",
                        f"No `{d.get('done_match')}` line within {float(d.get('max_minutes') or 0):g} min"],
                root_cause=(f"Job `{d.get('job')}` started but never reported completion. It is probably hung, "
                            "crashed mid-run, or is just slower than the limit set for it."),
                remediation="Check whether the job process is still running or stuck, look at the log after its "
                            "start line for a crash, and raise `max_minutes` if it is legitimately slow.",
                next_steps=["Check the process (still running? CPU/IO stuck?).",
                            "Read the log right after the start line for errors or a silent stop.",
                            "Re-run or resume the job if it is safe, then confirm its completion line appears."])


def _job_partial(signal, d, related):
    return dict(confidence=0.8, refs=[],
                checks=[f"Job `{d.get('job')}` reported {d.get('done')} of {d.get('total')} items done"],
                root_cause=(f"Job `{d.get('job')}` finished but only {d.get('done')} of {d.get('total')} items were "
                            "processed. The remaining items failed or were skipped; per-item errors may be in the log."),
                remediation="Find which items were not processed (look for per-item errors around the run) and "
                            "re-run for those items.",
                next_steps=["Search the log for errors from this run (bad recipient, timeout, validation).",
                            "Re-run only the failed items.",
                            "If this is expected, change the job's done pattern so it does not report it."])


def _job_missed(signal, d, related):
    if d.get("kind") == "unfinished":
        return _job_unfinished(signal, d, related)
    if d.get("kind") == "partial":
        return _job_partial(signal, d, related)
    errs = sorted({r.get("error") for r in related if r.get("error")})[:3]
    checks = [f"Schedule `{d.get('cron')}`; expected at {str(d.get('expected_at', ''))[:16]}",
              f"No log line matching `{d.get('match')}` within {float(d.get('grace_minutes') or 0):g} min",
              f"Last time the job was seen: {d.get('last_seen') or 'never since the agent started watching'}"]
    if errs:
        checks.append("Other errors from this service: " + ", ".join(f"`{e}`" for e in errs))
    return dict(confidence=0.7, refs=[], checks=checks,
                root_cause=(f"Job `{d.get('job')}` was due but never logged `{d.get('match')}`. The scheduler did not "
                            "start it, the app was down at that time, or the job crashed before logging."),
                remediation="Check the scheduler (crontab, systemd timer or the app's own scheduler) and that the app "
                            "was running at the due time, then run the job once by hand if it is safe.",
                next_steps=["Check the scheduler is running and the job is still registered.",
                            "Check the app was up (restarts, deploys) around the due time.",
                            "Look at the log around that time for a crash before the job started.",
                            "If the match text changed in a release, update it in the jobs file."])


def _tls(signal, d, related):
    return dict(confidence=0.8, refs=[], checks=[f"Matched in the log: `{d.get('matched', '')}`"],
                root_cause=(f"A TLS certificate is not acceptable (`{d.get('matched', '')}`): expired, self-signed, "
                            "for the wrong host, or the chain is incomplete. Either the server's own certificate or one "
                            "this app checks on another host."),
                remediation="Renew or replace the certificate (and install the full chain); if it is a self-signed "
                            "internal one, add its CA to the trust store instead of disabling verification.",
                next_steps=["Find which host's certificate it is (see the log lines).",
                            "Renew it, reload the service, and confirm the error stops.",
                            "Set HEALTH_TLS_HOSTS so the agent warns before the next expiry."])


def _security(signal, d, related):
    ips = sorted({ip for line in (signal.get("evidence") or []) for ip in _IP.findall(str(line))})[:5]
    checks = [f"{d.get('lines', 1)} matching line(s) in this batch", f"Matched in the log: `{d.get('matched', '')}`"]
    if ips:
        checks.append("Source address(es) seen: " + ", ".join(f"`{i}`" for i in ips))
    return dict(confidence=0.6, refs=[], checks=checks,
                root_cause=(f"Log lines look like abuse or probing (`{d.get('matched', '')}`). This is a pattern match, "
                            "not proof of an attack: a user mistyping a password looks similar."),
                remediation="Check whether the source is a real user; if not, block it at the firewall or WAF and "
                            "consider rate limiting or lockout on the login path.",
                next_steps=["Look at the source addresses and how many attempts there were.",
                            "Block or rate limit the source if it is hostile.",
                            "If an account was targeted, force a password reset and check for a successful login."])


def _database(signal, d, related):
    out = _server(signal, d, related)
    out["root_cause"] = (f"The database is failing the app (`{d.get('matched', '')}`): connections exhausted, a lock, "
                         "a missing table or pending migration, or the server is unreachable. "
                         + ("Reachability check: " + "; ".join(out["checks"]) + "." if out["checks"][:1] and
                            d.get("hosts") else ""))
    out["remediation"] = ("Check the database is up and has free connections, look for long-running locks, and run "
                          "any pending migration. A connection leak or missing index would be a code change to "
                          "ask for separately.")
    out["next_steps"] = ["Check the database server status and connection count.",
                         "Look for a recent migration or deploy that changed the schema.",
                         "Find long-running queries or locks and clear them if safe."]
    return out


def _deploy(signal, d, related):
    name = d.get("variable")
    refs = _code_refs(name) if name else []
    what = f"`{name}`" if name else f"`{d.get('matched', '')}`"
    return dict(confidence=0.75 if name else 0.6, refs=refs, checks=[f"Matched in the log: `{d.get('matched', '')}`"],
                root_cause=(f"The app cannot run correctly in this environment ({what}): a dependency is not installed, "
                            "a file or permission is missing, or the runtime version is wrong. This usually appears "
                            "right after a deploy."),
                remediation=("Install the missing dependency / fix the file permission on the server and restart. "
                             "If it was just deployed, check the build installed everything (lockfile, build step)."),
                next_steps=["Check what changed in the last deploy.",
                            "Install the dependency or fix the path or permission, then restart the app.",
                            "If it works locally but not here, compare runtime versions and installed packages."])


def _queue(signal, d, related):
    return dict(confidence=0.7, refs=[], checks=[f"Matched in the log: `{d.get('matched', '')}`"],
                root_cause=(f"Background processing is stuck or behind (`{d.get('matched', '')}`): workers are dead or "
                            "stalled, or jobs arrive faster than they are handled."),
                remediation="Check the workers are running, look at the queue depth and dead-letter queue, and restart "
                            "or scale the workers.",
                next_steps=["Check worker processes and their logs.",
                            "Look at queue depth and the dead-letter queue.",
                            "Restart or add workers, then confirm the backlog drains."])


BUILDERS = {"config": _config, "tls": _tls, "security": _security, "credentials": _credentials,
            "rate_limit": _rate_limit, "data": _data, "database": _database, "deploy": _deploy, "queue": _queue,
            "server": _server, "external": _external, "job_missed": _job_missed}


def diagnose(signal: dict, related: list[dict], classification: dict) -> dict:
    """Analysis for an operational error. Always ops-type: nothing here can start the coding agent."""
    category, details = classification["category"], classification.get("details") or {}
    out = BUILDERS[category](signal, details, related)
    refs = out["refs"]
    error = signal.get("error", "error")
    return {
        "notice": True,
        "summary": f"{classify.LABELS[category]}: {error} on {signal.get('service', '?')}",
        "impact": IMPACT[category],
        "category": category,
        "fix_type": "ops",
        "root_cause": out["root_cause"],
        "remediation": out["remediation"],
        "affected_files": [],
        "context_files": [],
        "estimated_lines": 0,
        "code_refs": refs,
        "checks": out["checks"],
        "evidence": list(signal.get("evidence", []))[:5],
        "confidence": out["confidence"],
        "risk": "low",
        "required_tests": [],
        "next_steps": out["next_steps"],
        "classified_by": "rules",
    }


def build_notice_card(signal: dict, analysis: dict, occurrences: int, signal_id: str = "",
                      issue: dict | None = None) -> tuple[str, str]:
    """'Action needed' card: what happened, what was checked, what to do. No code-fix approval."""
    service, severity, error = signal.get("service", "?"), signal.get("severity", "?"), signal.get("error", "?")
    bullets = lambda items: NL.join(f"• {i}" for i in items) or "• none"  # noqa: E731
    refs = analysis.get("code_refs") or []
    ref_lines = ["`" + r["path"] + ":" + str(r["line"]) + "`" for r in refs]
    seen = f"{issues.describe(issue['data'])}{NL}" if issue else ""
    hours = issues.remind_interval().total_seconds() / 3600
    quiet = issues.quiet_window().total_seconds() / 60
    body = (
        f"*INCIDENT*{NL}{seen}"
        f"Signal `{signal_id or '?'}` · {occurrences} occurrence(s) · "
        f"{len(signal.get('evidence', []) or [])} log line(s) in this signal · last seen {signal.get('ts', '?')}{NL}{NL}"
        f"*SUMMARY*{NL}{analysis.get('summary')}{NL}{NL}"
        f"*IMPACT*{NL}{analysis.get('impact')}{NL}{NL}"
        f"*ROOT CAUSE*  (category: {analysis.get('category')} · confidence {round(analysis.get('confidence', 0) * 100)}%){NL}"
        f"{analysis.get('root_cause')}{NL}{NL}"
        f"*WHAT WAS CHECKED*{NL}{bullets(analysis.get('checks') or [])}{NL}{NL}"
        + (f"*WHERE IN THE CODE*{NL}{bullets(ref_lines)}{NL}{NL}" if refs else "")
        + f"*LOG EVIDENCE*{NL}{bullets(pretty_line(e) for e in (signal.get('evidence') or [])[:6])}{NL}{NL}"
        f"*WHAT TO DO*{NL}{analysis.get('remediation')}{NL}{NL}"
        f"*NEXT STEPS*{NL}{bullets(analysis.get('next_steps') or [])}{NL}{NL}"
        f"*ON APPROVE*{NL}"
        f"• This is not a code problem, so there is no code fix to approve.{NL}"
        f"• I'll fix this: you handle it. You are reminded every {hours:g} h until the error has stopped for "
        f"{quiet:g} min.{NL}"
        f"• Ignore: repeats are counted silently; you get a new card only if it occurs more than "
        f"{issues.ignore_threshold()} more times."
    )
    return f"action needed: {service} {severity} {error}", body
