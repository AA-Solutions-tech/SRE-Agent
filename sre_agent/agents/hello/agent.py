"""Smallest useful agent: an LLM with GitHub + Jira tools (ReAct loop via LangGraph).

    python run.py hello "which PRs are open in owner/repo?"
"""
from langgraph.prebuilt import create_react_agent

from sre_agent.core.config import settings
from sre_agent.core.llm import get_llm, text_of
from sre_agent.core.tools import github_tools, jira_tools

SYSTEM = "You are a helpful engineering assistant. Use the tools to answer; be concise."


def build():
    tools = github_tools() + (jira_tools() if settings.jira_base_url else [])
    return create_react_agent(get_llm(), tools, prompt=SYSTEM)


def run(question: str) -> str:
    result = build().invoke({"messages": [("user", question)]})
    return text_of(result["messages"][-1])
