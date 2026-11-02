"""Shared contracts for Agent 3 (Coding Agent).

Single source of truth for Supervisor <-> Coding JSON handoffs.
Slack summaries are always DERIVED from these dicts, never parsed by agents.

Flow (locked):
  Investigation --JSON--> Supervisor --summary--> Slack [Approve]
  --decision--> Supervisor --CodingInput JSON--> Coding
  --CodingResult JSON--> Supervisor --VerifyTask JSON--> Monitor (verify-only)
"""
from typing import TypedDict


class Signal(TypedDict, total=False):
    """Frozen Monitor contract (mirror of agents/monitor/parse.build_signal)."""

    service: str
    severity: str
    error: str
    evidence: list[str]
    ts: str
    source: str
    quarantine: list[str]


class InvestigationReport(TypedDict, total=False):
    """What the Investigation Agent must emit (stub until Agent 2 lands)."""

    incident_id: str
    root_cause: str
    evidence: list[str]
    affected_files: list[str]  # repo-relative paths
    remediation: str  # concrete fix description
    confidence: float  # 0.0 - 1.0
    required_tests: list[str]
    risks: list[str]


class TestReport(TypedDict):
    command: str
    exit_code: int | None
    passed: bool
    output_tail: str


class CodingResult(TypedDict, total=False):
    incident_id: str
    status: str  # "completed" | "blocked" | "failed"
    root_cause_ref: str
    changed_files: list[str]
    summary: str
    tests: list[TestReport]
    validation: dict
    remaining_risks: list[str]
    requires_approval: bool
    worktree: str
    branch: str
    failure_reason: str
    clarification_needed: list[str]


class VerifyTask(TypedDict, total=False):
    """Supervisor -> Monitor re-check (does the error still fire after the fix?)."""

    incident_id: str
    service: str
    error_signature: str
    fixed_at: str
    window_seconds: int


class VerifyResult(TypedDict, total=False):
    incident_id: str
    verdict: str  # "resolved" | "persisting" | "inconclusive"
    new_evidence: list[str]
    checked_lines: int
