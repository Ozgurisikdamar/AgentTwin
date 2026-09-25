"""agenttwin_core: the shared runtime of AgentTwin's Python services.

The Go services use ``packages/gokit``; this package mirrors its conventions
(configuration rules, log fields, error envelope, internal tokens, RBAC,
migration ledger, event topology) so both languages behave identically.
"""

__version__ = "0.1.0"
