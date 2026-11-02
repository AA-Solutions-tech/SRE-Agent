"""Data sources for the manager report. Each returns plain dicts/strings the LLM can summarize."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from sre_agent.core.config import settings


def github_activity(days: int = 7) -> list[dict]:
    """Commits + PRs per repo in the last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    headers = {"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json"}
    out = []
    for repo in settings.github_repos:
        base = f"https://api.github.com/repos/{repo}"
        commits = httpx.get(f"{base}/commits", params={"since": since, "per_page": 100}, headers=headers, timeout=30).json()
        prs = httpx.get(f"{base}/pulls", params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 30}, headers=headers, timeout=30).json()
        out.append({
            "repo": repo,
            "commits": [{"author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                         "message": c["commit"]["message"].splitlines()[0]} for c in commits if isinstance(c, dict)],
            "pull_requests": [{"title": p["title"], "user": p["user"]["login"], "state": p["state"],
                               "merged": bool(p.get("merged_at"))} for p in prs if isinstance(p, dict) and p["updated_at"] >= since],
        })
    return out


def jira_activity(days: int = 7) -> list[dict]:
    """Issues updated in the last N days."""
    if not settings.jira_base_url:
        return []
    projects = ",".join(settings.jira_projects)
    jql = f"updated >= -{days}d" + (f" AND project in ({projects})" if projects else "") + " ORDER BY updated DESC"
    r = httpx.get(
        f"{settings.jira_base_url}/rest/api/3/search/jql",
        params={"jql": jql, "fields": "summary,status,assignee", "maxResults": 100},
        auth=(settings.jira_email, settings.jira_api_token), timeout=30,
    )
    r.raise_for_status()
    return [{"key": i["key"], "summary": i["fields"]["summary"], "status": i["fields"]["status"]["name"],
             "assignee": (i["fields"].get("assignee") or {}).get("displayName", "unassigned")} for i in r.json().get("issues", [])]


def standup_updates(path: str = "data/standups.jsonl") -> list[dict]:
    """Daily 'done yesterday / plan today' messages. STUB: one JSON object per line
    {"user":..., "date":..., "text":...}. Replace with Google Chat API later (needs workspace auth)."""
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
