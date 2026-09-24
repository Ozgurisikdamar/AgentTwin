from __future__ import annotations

from itertools import pairwise

import pytest

from agenttwin_core.jobs import InvalidTransition, JobStatus, allowed_from, check_transition


def test_happy_path_transitions() -> None:
    path = [
        JobStatus.QUEUED,
        JobStatus.PREPARING,
        JobStatus.RUNNING,
        JobStatus.EVALUATING,
        JobStatus.COMPLETED,
    ]
    for a, b in pairwise(path):
        check_transition(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (JobStatus.COMPLETED, JobStatus.RUNNING),
        (JobStatus.CANCELLED, JobStatus.QUEUED),
        (JobStatus.FAILED, JobStatus.COMPLETED),
        (JobStatus.QUEUED, JobStatus.COMPLETED),
        (JobStatus.RUNNING, JobStatus.COMPLETED),
        (JobStatus.EVALUATING, JobStatus.CANCELLED),
    ],
)
def test_invalid_transitions_fail(a: JobStatus, b: JobStatus) -> None:
    with pytest.raises(InvalidTransition):
        check_transition(a, b)


def test_terminal_states_have_no_exit() -> None:
    for s in JobStatus:
        if s.terminal:
            for t in JobStatus:
                with pytest.raises(InvalidTransition):
                    check_transition(s, t)


def test_allowed_from_for_sql_guards() -> None:
    assert allowed_from(JobStatus.CANCELLED) == ["PREPARING", "QUEUED", "RUNNING"]
    assert allowed_from(JobStatus.COMPLETED) == ["EVALUATING"]
    assert allowed_from(JobStatus.QUEUED) == ["EVALUATING", "PREPARING", "RUNNING"]
