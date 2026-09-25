"""Helpers shared by the SDK tests (importable because conftest adds this
directory to sys.path; the tests directory is not a package)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hypothesis import strategies as st

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "packages" / "contracts" / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def json_values() -> st.SearchStrategy[Any]:
    scalars = (
        st.none()
        | st.booleans()
        | st.integers(min_value=-(2**70), max_value=2**70)
        | st.floats(allow_nan=False, allow_infinity=False)
        | st.text(max_size=20)
    )
    return st.recursive(
        scalars,
        lambda children: (
            st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=6), children, max_size=4)
        ),
        max_leaves=12,
    )


# Text that tends to contain near-misses of every rule.
FRAGMENTS = st.sampled_from(
    [
        "jane.doe@example.com",
        "password=",
        "secret: ",
        "Bearer ",
        "eyJhbGciOiJIUzI1NiJ9.",
        "atk_ab12cd34_",
        "sk-ant-api03-",
        "4111 1111 1111 1111",
        "+44 20 7946 0958",
        "(415) 555-2671",
        "[REDACTED:email]",
        "[HASH:card:0123456789ab]",
        "ORD-1001",
        "150.00 USD",
        " ",
        "\n",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----END RSA PRIVATE KEY-----",
    ]
)
texty = st.lists(FRAGMENTS | st.text(max_size=12), max_size=12).map("".join)
