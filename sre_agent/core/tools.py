"""Ready-made LangChain tools for GitHub, Jira and chat. Hand any of these to an agent.

    from sre_agent.core.tools import github_tools, jira_tools, chat_tools
    agent = create_react_agent(get_llm(), github_tools() + jira_tools())

Write-capable tools (create_*/comment/post) are separated so you can give an agent read-only access by default.
"""
import httpx
from langchain_core.tools import tool

from .config import settings
from .notify import notify

_GH = "https://api.github.com"


def _gh_headers() -> dict:
    return {"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json"}


def _jira_auth() -> tuple[str, str]:
    return settings.jira_email, settings.jira_api_token


def _adf(text: str) -> dict:
    """Plain text -> Atlassian Document Format (required by Jira v3 for descriptions/comments)."""
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


# ---------------- GitHub ----------------
def github_tools(write: bool = False) -> list:
    @tool
    def gh_list_prs(repo: str, state: str = "open") -> str:
        """List pull requests of a repo ('owner/name'). state = open | closed | all."""
        r = httpx.get(f"{_GH}/repos/{repo}/pulls", params={"state": state, "per_page": 30}, headers=_gh_headers(), timeout=30)
        r.raise_for_status()
        return "\n".join(f"#{p['number']} [{p['state']}] {p['title']} (by {p['user']['login']})" for p in r.json()) or "none"

    @tool
    def gh_get_pr_diff(repo: str, number: int) -> str:
        """Get the unified diff of a pull request (truncated to 15k chars)."""
        h = {**_gh_headers(), "Accept": "application/vnd.github.diff"}
        r = httpx.get(f"{_GH}/repos/{repo}/pulls/{number}", headers=h, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.text[:15_000]

    @tool
    def gh_list_issues(repo: str, state: str = "open") -> str:
        """List issues (not PRs) of a repo ('owner/name')."""
        r = httpx.get(f"{_GH}/repos/{repo}/issues", params={"state": state, "per_page": 30}, headers=_gh_headers(), timeout=30)
        r.raise_for_status()
        return "\n".join(f"#{i['number']} {i['title']}" for i in r.json() if "pull_request" not in i) or "none"

    tools = [gh_list_prs, gh_get_pr_diff, gh_list_issues]

    if write:
        @tool
        def gh_comment(repo: str, number: int, body: str) -> str:
            """Comment on a GitHub issue or PR."""
            r = httpx.post(f"{_GH}/repos/{repo}/issues/{number}/comments", json={"body": body}, headers=_gh_headers(), timeout=30)
            r.raise_for_status()
            return r.json()["html_url"]

        tools.append(gh_comment)
    return tools


# ---------------- Jira ----------------
def jira_tools(write: bool = False) -> list:
    base = settings.jira_base_url

    @tool
    def jira_search(jql: str) -> str:
        """Search Jira issues with JQL, e.g. 'project = ABC AND status = "In Progress"'."""
        r = httpx.get(f"{base}/rest/api/3/search/jql", params={"jql": jql, "fields": "summary,status,assignee", "maxResults": 25},
                      auth=_jira_auth(), timeout=30)
        r.raise_for_status()
        return "\n".join(
            f"{i['key']} [{i['fields']['status']['name']}] {i['fields']['summary']} "
            f"({(i['fields'].get('assignee') or {}).get('displayName', 'unassigned')})"
            for i in r.json().get("issues", [])) or "none"

    @tool
    def jira_get_issue(key: str) -> str:
        """Get a Jira issue's summary, status and description."""
        r = httpx.get(f"{base}/rest/api/3/issue/{key}", params={"fields": "summary,status,description"}, auth=_jira_auth(), timeout=30)
        r.raise_for_status()
        f = r.json()["fields"]
        return f"{key} [{f['status']['name']}] {f['summary']}\n{str(f.get('description'))[:3000]}"

    tools = [jira_search, jira_get_issue]

    if write:
        @tool
        def jira_create_issue(project: str, summary: str, description: str = "", issue_type: str = "Task") -> str:
            """Create a Jira issue. Returns its key."""
            body = {"fields": {"project": {"key": project}, "summary": summary,
                               "description": _adf(description or summary), "issuetype": {"name": issue_type}}}
            r = httpx.post(f"{base}/rest/api/3/issue", json=body, auth=_jira_auth(), timeout=30)
            r.raise_for_status()
            return r.json()["key"]

        @tool
        def jira_comment(key: str, text: str) -> str:
            """Add a comment to a Jira issue."""
            r = httpx.post(f"{base}/rest/api/3/issue/{key}/comment", json={"body": _adf(text)}, auth=_jira_auth(), timeout=30)
            r.raise_for_status()
            return "commented"

        tools += [jira_create_issue, jira_comment]
    return tools


# ---------------- Chat ----------------
def chat_tools() -> list:
    @tool
    def post_to_chat(title: str, message: str) -> str:
        """Post a message to the team's configured chat/email channels (Slack, Google Chat, email)."""
        notify(title, message)
        return "sent"

    return [post_to_chat]
