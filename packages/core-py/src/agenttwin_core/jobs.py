"""The run state machine shared by simulation and evaluation runs (spec §86,
ADR-0006). Transitions are validated in code *and* enforced by a guarded SQL
update (``allowed_from``), so a stale worker can never move a run backwards.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["InvalidTransition", "JobStatus", "allowed_from", "check_transition"]


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})

# QUEUED appears as a target of the active states: an expired lease returns a
# run to the queue for another worker (bounded by the attempt counter).
_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.PREPARING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.PREPARING: frozenset(
        {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED}
    ),
    JobStatus.RUNNING: frozenset(
        {JobStatus.EVALUATING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED}
    ),
    JobStatus.EVALUATING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.QUEUED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        super().__init__(f"invalid run transition {current} -> {target}")
        self.current = current
        self.target = target


def check_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in _TRANSITIONS[current]:
        raise InvalidTransition(current, target)


def allowed_from(target: JobStatus) -> list[str]:
    """The states a run may be in for ``target`` to be reachable (for SQL guards)."""
    return sorted(str(s) for s, targets in _TRANSITIONS.items() if target in targets)
