"""Weekly manager report: GitHub + Jira + standups -> LLM summary -> Slack/GChat/email.

    python run.py manager_report --days 7
"""
import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from sre_agent.core.llm import get_llm, text_of
from sre_agent.core.notify import notify

from .sources import github_activity, jira_activity, standup_updates

PROMPT = """You write a weekly engineering status report for a manager.
Sections: Highlights, Per-person progress, Risks/blockers, Next week. Be concrete; cite PR titles / Jira keys.
Do not invent anything that is not in the data.

DATA:
{data}"""


class State(TypedDict, total=False):
    days: int
    data: dict
    report: str


def gather(state: State) -> State:
    days = state.get("days", 7)
    return {"data": {"github": github_activity(days), "jira": jira_activity(days), "standups": standup_updates()}}


def summarize(state: State) -> State:
    data = json.dumps(state["data"], default=str)[:60_000]
    return {"report": text_of(get_llm().invoke(PROMPT.format(data=data)))}


def publish(state: State) -> State:
    notify("Weekly engineering report", state["report"])
    return {}


def build():
    g = StateGraph(State)
    g.add_node("gather", gather)
    g.add_node("summarize", summarize)
    g.add_node("publish", publish)
    g.add_edge(START, "gather")
    g.add_edge("gather", "summarize")
    g.add_edge("summarize", "publish")
    g.add_edge("publish", END)
    return g.compile()


def run(days: int = 7) -> str:
    return build().invoke({"days": days})["report"]
