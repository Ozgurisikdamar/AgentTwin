# ADR-0014 — Pluggable embeddings with an honest local default

* Status: accepted · Date: 2026-09-24

## Context
The regression miner and scenario selection use vector similarity. CI must run offline.

## Decision
`EmbeddingProvider.embed(texts) -> vectors` with two implementations:
* `hashing-v1` (default, local): signed feature hashing of word and character n-grams of
  a **deterministic structured trace summary**, 256 dimensions, L2-normalized. It measures
  *lexical/structural* similarity and is labeled as such in the UI ("text similarity"),
  never as semantic understanding.
* `openai-compatible` (optional): any `/v1/embeddings` endpoint (OpenAI, TEI, Ollama, vLLM).
  When enabled, the UI may say "semantic similarity".
Each stored vector records `embedding_model`; similarity is computed only among equal models.
Clustering: threshold-based nearest-neighbour grouping for small sets; HDBSCAN
(scikit-learn) once a project has at least 50 failures, keeping noise points unclustered.

## Consequences
No external dependency for the default path; quality upgrades are a configuration change
plus a re-embedding job.
