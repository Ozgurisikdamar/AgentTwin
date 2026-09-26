"""The ``evaluation-service`` command (see agenttwin_evaluation.main for its roles).

``healthcheck`` is answered here, before the service is imported: the
container runs it every few seconds, and importing the service costs about
1.5 CPU seconds a time (agenttwin_core.probe).
"""

from __future__ import annotations

import os
import sys

from agenttwin_core.probe import healthcheck

# The service's default port (SPEC.default_port; a test holds them equal).
DEFAULT_PORT = 8083


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["healthcheck"]:
        sys.exit(healthcheck(os.environ.get("PORT"), DEFAULT_PORT, args[1:]))
    # The whole service, imported only when it is about to run.
    from agenttwin_evaluation.main import main as run

    run()
