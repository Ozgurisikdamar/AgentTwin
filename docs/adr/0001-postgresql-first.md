# ADR-0001 — PostgreSQL (+pgvector) instead of specialized graph/vector/trace stores

* Status: accepted · Date: 2026-09-24

## Context
AgentTwin stores traces/spans, a dependency graph, embeddings of failures and
scenarios, and relational governance data. Specialized stores exist for each
(ClickHouse, Neo4j, Qdrant). Each adds an operational surface for a small team.

## Decision
One PostgreSQL 16 cluster with one logical schema per service:

* **Traces**: `trace.trace` / `trace.span` with extracted, indexed filter columns and
  a JSONB attribute bag. Cursor pagination on `(started_at, trace_id)`.
* **Graph**: adjacency tables (`graph.component`, `graph.dependency_edge`,
  `graph.edge_evidence`) and **bounded recursive CTEs** (default depth 4) with
  per-path cycle detection.
* **Vectors**: `pgvector` columns (`vector(256)`) with the embedding model id stored
  next to each vector; similarity is only computed between vectors of the same model.

## Upgrade triggers (measured, not speculative)
* **ClickHouse** for trace analytics when, after correct indexing/partitioning, trace
  queries miss their SLO, *or* sustained ingestion reaches several hundred spans/s and
  grows, *or* stored spans exceed ~10M and analytical queries measurably compete with
  transactional load.
* **Qdrant** when the embedding corpus exceeds a comfortable pgvector range or filtered
  ANN queries become a measured bottleneck.
* **Neo4j / graph DB** when bounded traversals are a proven bottleneck or graph
  algorithms (not bounded traversal) become central.

## Consequences
One backup/restore story, transactional consistency inside each service, simpler local
development. Span partitioning by time is deferred until volume justifies it.
