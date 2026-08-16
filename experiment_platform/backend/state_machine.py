"""Run state machine (task §59).

States and allowed transitions. Every transition is persisted via the Database.
"""
from __future__ import annotations

STATES = [
    "DRAFT", "PREPARING", "WAITING_GNB", "SYNCING_PHONE", "ARMED",
    "PHONE_OFFLINE", "RUNNING", "WAITING_PHONE_RETURN", "IMPORTING",
    "ALIGNING", "COMPLETE", "WARNING", "FAILED", "STOPPED",
]

# Allowed forward transitions (terminal states COMPLETE/WARNING/FAILED/STOPPED sink).
# Any non-terminal state may be stopped by the user.
ALLOWED: dict[str, set[str]] = {
    "DRAFT": {"PREPARING", "FAILED", "STOPPED"},
    "PREPARING": {"WAITING_GNB", "FAILED", "STOPPED"},
    "WAITING_GNB": {"SYNCING_PHONE", "FAILED", "STOPPED"},
    "SYNCING_PHONE": {"ARMED", "FAILED", "STOPPED"},
    "ARMED": {"PHONE_OFFLINE", "RUNNING", "FAILED", "STOPPED"},
    "PHONE_OFFLINE": {"RUNNING", "FAILED", "STOPPED"},
    "RUNNING": {"WAITING_PHONE_RETURN", "WARNING", "FAILED", "STOPPED"},
    "WAITING_PHONE_RETURN": {"IMPORTING", "FAILED", "STOPPED"},
    "IMPORTING": {"ALIGNING", "FAILED", "STOPPED"},
    "ALIGNING": {"COMPLETE", "WARNING", "FAILED", "STOPPED"},
    "COMPLETE": set(),
    "WARNING": set(),
    "FAILED": set(),
    "STOPPED": set(),
}


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in ALLOWED.get(from_state, set())


def next_state_after_failure(state: str) -> str:
    return "FAILED"
