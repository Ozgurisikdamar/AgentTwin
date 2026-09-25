from __future__ import annotations

import uuid

from agenttwin_core.ids import new_id, valid_uuid


def test_uuid7_layout() -> None:
    u = uuid.UUID(new_id())
    assert u.version == 7
    assert u.variant == uuid.RFC_4122


def test_ids_are_unique_and_time_ordered() -> None:
    ids = [new_id() for _ in range(20_000)]
    assert len(set(ids)) == len(ids)
    # Monotonic within the process: string order equals generation order.
    assert ids == sorted(ids)


def test_valid_uuid() -> None:
    assert valid_uuid(new_id())
    assert not valid_uuid("not-a-uuid")
    assert not valid_uuid(new_id().replace("-", ""))
    assert not valid_uuid(123)
