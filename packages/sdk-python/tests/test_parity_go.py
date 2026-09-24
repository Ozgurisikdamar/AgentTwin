"""Differential tests: the Python implementation must agree with the Go
reference implementation (gokit/hashx, gokit/redact) on thousands of random
inputs, not only on the hand-written fixtures.

Inputs come from a seeded generator (``AGENTTWIN_PARITY_SEED``) so a failure
is reproducible; the seed is part of every assertion message.
"""

from __future__ import annotations

import os
import random
import string
from typing import Any

from agenttwin.hashing import canonical_json
from agenttwin.redaction import Redactor

SEED = int(os.environ.get("AGENTTWIN_PARITY_SEED", "20260924"))
CASES = int(os.environ.get("AGENTTWIN_PARITY_CASES", "3000"))

FRAGMENTS = [
    "jane.doe@example.com",
    "x@y.io",
    "password=",
    "passwd: ",
    "secret=",
    "api_key: '",
    "access-token=",
    "Bearer ",
    "bearer\t",
    "eyJhbGciOiJIUzI1NiJ9.",
    "eyJzdWIiOiIxMjMifQ.",
    "atk_ab12cd34_",
    "sk-ant-api03-",
    "AKIA",
    "ghp_",
    "xoxb-",
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "1234 5678 9012 3456",
    "+44 20 7946 0958",
    "+1.415.555.2671",
    "(415) 555-2671",
    "415-555-2671",
    "[REDACTED:email]",
    "[HASH:card:0123456789ab]",
    "[REDACTED:",
    "ORD-1001",
    "150.00 USD",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----END RSA PRIVATE KEY-----",
    "ş",
    "é",
    "\u2028",
    "\x00",
]
ALPHABET = string.ascii_letters + string.digits + " \t\n.,:;=@+-_/'\"()[]{}"


def random_text(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 10)):
        if rng.random() < 0.55:
            parts.append(rng.choice(FRAGMENTS))
        else:
            parts.append("".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 16))))
    return "".join(parts)


def random_json(rng: random.Random, depth: int = 0) -> Any:
    roll = rng.random()
    if depth >= 3 or roll < 0.45:
        kind = rng.randint(0, 6)
        if kind == 0:
            return None
        if kind == 1:
            return rng.random() < 0.5
        if kind == 2:
            return rng.randint(-(2**70), 2**70) if rng.random() < 0.2 else rng.randint(-1000, 1000)
        if kind == 3:
            exp = rng.choice([-30, -8, -5, -4, -3, 0, 3, 5, 6, 7, 15, 20, 21, 22, 300])
            return rng.uniform(-10, 10) * 10.0**exp
        if kind == 4:
            return float(rng.randint(-(10**6), 10**6))
        return random_text(rng)
    if roll < 0.72:
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {random_text(rng)[:8]: random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def test_redaction_agrees_with_go(go_parity: Any) -> None:
    rng = random.Random(SEED)
    inputs = [random_text(rng) for _ in range(CASES)]
    for mode in ("all", "secrets"):
        for strategy in ("mask", "hash"):
            reqs = [{"op": "redact", "mode": mode, "strategy": strategy, "input": s} for s in inputs]
            go = go_parity(reqs)
            red = Redactor.all(strategy) if mode == "all" else Redactor.secrets(strategy)  # type: ignore[arg-type]
            for s, g in zip(inputs, go, strict=True):
                assert red.text(s)[0] == g["output"], (
                    f"seed={SEED} mode={mode} strategy={strategy} input={s!r}"
                )


def test_canonical_json_agrees_with_go(go_parity: Any) -> None:
    rng = random.Random(SEED + 1)
    values = [random_json(rng) for _ in range(CASES)]
    # json.dumps writes floats with repr(), so Go parses exactly the number
    # text Python holds.
    go = go_parity([{"op": "canonical", "value": v} for v in values])
    for v, g in zip(values, go, strict=True):
        assert not g.get("error"), g
        assert canonical_json(v) == g["output"], f"seed={SEED + 1} value={v!r}"
