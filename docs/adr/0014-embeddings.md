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

## Implementation note (2026-09-25, Phase 4)
* `hashing-v1` (`agenttwin_core.embeddings`) hashes three kinds of features: stemmed
  words (a light suffix stripper, so "refunds"/"refunded" agree), whole identifiers
  besides their words (`refund_payment` is a feature of its own, as are `lookupOrder` →
  `lookup_order` and `cross-tenant`), and pairs of neighbouring words at half weight;
  stopwords are dropped, term frequency is sublinear. The character n-grams of the first
  draft are not used: the stemmer covers the word forms they were meant for, and adding
  them later is a new model name. Hashing uses BLAKE2b, never Python's `hash()`, so a
  vector does not depend on the process; a test pins the vector of a fixed text, and a
  change to the features must change the model name.
* The first user is scenario selection (spec §22): the simulation service stores one
  vector per scenario (`simulation.scenario_embedding`, pgvector, exact search per
  project — a project's library is small; an ANN index is an upgrade for when it is not,
  ADR-0001) together with the model, the dimension and the *recipe* of the text it was
  computed from (`scenario-text-v1`: name, description, tags, what it covers, the
  customer's message, faults and expectations). Vectors are computed lazily, when
  scenarios are next matched; a row of another model, recipe or scenario version is
  computed again. `POST /api/v1/scenarios/match` answers each selected scenario with the
  reasons it was selected for.
* The provider interface is async (`await embed(texts)`) because a hosted provider is
  a network call. `OpenAICompatibleEmbedder` (`EMBEDDING_PROVIDER=openai_compatible`)
  posts to `{EMBEDDING_BASE_URL}/embeddings` in batches (`EMBEDDING_BATCH_SIZE`, 32):
  plain httpx, no proxies from the environment and no redirects (the key goes to the
  configured endpoint only), blank texts not sent (zero vector, as with hashing),
  texts cut at 20 000 characters. Every answer is checked before use — one vector per
  text matched by `index`, each of exactly `EMBEDDING_DIMENSIONS` finite numbers —
  and a provider that is down, slow or rate limited is retried with backoff
  (`Retry-After` up to 10 s); a refusal is not. `EMBEDDING_MODEL` and
  `EMBEDDING_DIMENSIONS` are required (the model name must not claim `hashing`);
  `EMBEDDING_API_KEY` is required only by api.openai.com, so local servers work
  without one; `EMBEDDING_SEND_DIMENSIONS` sends `dimensions` for models that shorten
  their vectors. Changing the model re-embeds lazily (stored rows of another model
  are stale).
* A provider that cannot answer makes `POST /api/v1/scenarios/match` answer `503
  EMBEDDINGS_UNAVAILABLE` — never an empty selection — so a change impact reports the
  simulation service as a problem (`complete: false`).
