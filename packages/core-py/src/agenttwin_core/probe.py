"""The container HEALTHCHECK probe of a Python service (``<svc> healthcheck``).

Standard library ``socket`` only, on purpose. The container runs the probe
every few seconds, and what it imports is paid every time: a probe that
imported the service (FastAPI, the database driver, the evaluators) cost
1.4-1.5 CPU seconds a call, about a third of a core per container all the
time, and even ``urllib.request`` (which brings ``ssl``, ``email`` and
``http.client``) costs a quarter of a second (docs/benchmarks/load-baseline.md).
The services' command entry points answer ``healthcheck`` from here before
they import anything else.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Sequence

PATHS = {"ready": "/health/ready", "live": "/health/live"}


def status(port: int, path: str, timeout_s: float = 3.0) -> int:
    """The HTTP status the local process answers ``GET path`` with. Direct,
    never through a proxy; raises OSError when it cannot be asked."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout_s) as conn:
        conn.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("ascii"))
        head = b""
        while b"\r\n" not in head and len(head) < 1024:
            chunk = conn.recv(1024)
            if not chunk:
                break
            head += chunk
    parts = head.split(b"\r\n", 1)[0].split()
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        raise OSError(f"not an HTTP answer: {head[:40]!r}")
    return int(parts[1])


def healthcheck(port_env: str | None, default_port: int, args: Sequence[str]) -> int:
    """Probes the local process (readiness by default); returns the exit code:
    0 healthy, 1 not (or not answering), 2 misused."""
    port = default_port
    if port_env:
        if not port_env.isdigit() or not 1 <= int(port_env) <= 65535:
            print(f"healthcheck: invalid PORT {port_env!r}", file=sys.stderr)  # noqa: T201
            return 2
        port = int(port_env)
    if len(args) > 1 or (args and args[0] not in PATHS):
        print("usage: healthcheck [live|ready]", file=sys.stderr)  # noqa: T201
        return 2
    path = PATHS[args[0] if args else "ready"]
    try:
        code = status(port, path)
    except OSError as err:
        print(f"healthcheck: {err}", file=sys.stderr)  # noqa: T201
        return 1
    if code != 200:
        print(f"healthcheck: {path} answered {code}", file=sys.stderr)  # noqa: T201
        return 1
    return 0
