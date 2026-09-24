# ADR-0000 — Product name: AgentTwin

* Status: accepted · Date: 2026-09-24

## Context
The build specification uses the working name *AegisTwin* (CLI `aegis`, manifest
`aegistwin.dev/v1`). The repository was created as **AgentTwin** with the tagline
"Ship agents with evidence, not hope." The specification (§6) says: *"If the
repository already has a name, preserve it and use the architecture/product
specification regardless."*

## Decision
The product is **AgentTwin** everywhere:

| Spec (working name) | This repository |
|---|---|
| AegisTwin | AgentTwin |
| `aegis` CLI | `agenttwin` CLI |
| `aegistwin.dev/v1` | `agenttwin.dev/v1` |
| `aegistwin` Python SDK | `agenttwin` Python SDK |
| `x-aegis-*` headers | `x-agenttwin-*` headers |

## Consequences
Architecture, domain and behavior follow the specification unchanged; only names differ.
