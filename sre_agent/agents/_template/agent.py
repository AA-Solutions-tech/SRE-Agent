"""TEMPLATE - copy this folder:  cp -r agents/_template agents/my_agent   (then edit agent.py)

Pattern: LangGraph pipeline  analyze (LLM + tools)  ->  propose  ->  HUMAN APPROVAL  ->  act.
  * Agents only *propose*. Anything with side effects (push, create ticket, send) lives in the
    approval handler, which runs only after a human clicks the signed Approve link.
  * Search for TODO(you) to find the spots to change.

    python run.py my_agent "some input"
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

from sre_agent.core.approvals import on_approval, request_approval
from sre_agent.core.db import create_item, get_item
from sre_agent.core.llm import get_llm, text_of
from sre_agent.core.notify import notify
from sre_agent.core.tools import github_tools  # TODO(you): add jira_tools(), chat_tools(), git_tools.make_read_tools(...)

STAGE = "act"  # name of the approval gate; appears in the approval URL

SYSTEM = "TODO(you): describe the agent's job, rules and output format."


class State(TypedDict, total=False):
    input: str
    analysis: str
    item_id: str


def analyze(state: State) -> State:
    """Step 1: LLM with READ-ONLY tools investigates the input."""
    agent = create_react_agent(get_llm(), github_tools(), prompt=SYSTEM)  # TODO(you): tools
    out = agent.invoke({"messages": [("user", state["input"])]})
    return {"analysis": text_of(out["messages"][-1])}


def propose(state: State) -> State:
    """Step 2: store the proposal and ask a human to approve it."""
    item_id = create_item("template", {"input": state["input"], "proposal": state["analysis"]})
    request_approval(item_id, STAGE, "Agent proposal - approve?", state["analysis"])
    return {"item_id": item_id}


@on_approval(STAGE)
def act(item_id: str, action: str) -> str:
    """Step 3: runs when a human clicks Approve/Reject. Put side effects HERE (plain code)."""
    item = get_item(item_id)
    if action == "reject":
        return "Rejected - nothing done."
    # TODO(you): do the real thing with item["data"]["proposal"] (open a PR, create Jira ticket, ...)
    notify("Approved action executed", item["data"]["proposal"][:500])
    return "Approved - action executed."


def build():
    g = StateGraph(State)
    g.add_node("analyze", analyze)
    g.add_node("propose", propose)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "propose")
    g.add_edge("propose", END)
    return g.compile()


def run(text: str) -> str:
    out = build().invoke({"input": text})
    return f"proposal {out['item_id']} sent for approval:\n{out['analysis']}"
