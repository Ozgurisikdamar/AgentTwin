-- Scenario embeddings (Phase 4): one vector per scenario, for choosing the
-- scenarios a change is semantically close to (spec §22 "Semantic scenario
-- matching"). pgvector is installed in public, which every service's
-- search_path (<schema>, public) reaches; another service may install it
-- too, so installing is serialized under one advisory lock.
SELECT pg_advisory_xact_lock(hashtext('agenttwin-extension-vector'));
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- An embedding belongs to the scenario's organization and project; the
-- foreign key over all three makes one stored under another tenant
-- impossible, so a similarity search never crosses tenants.
ALTER TABLE scenario ADD CONSTRAINT scenario_tenancy UNIQUE (id, organization_id, project_id);

-- The vector of a scenario's latest version, computed when scenarios are
-- matched. It is stored with the model that produced it and only vectors of
-- the model in use are compared; a row of another model, version or text
-- recipe is computed again.
CREATE TABLE scenario_embedding (
    scenario_id     uuid PRIMARY KEY,
    organization_id uuid NOT NULL,
    project_id      uuid NOT NULL,
    version         int NOT NULL CHECK (version > 0),
    recipe          text NOT NULL CHECK (recipe ~ '^[a-z0-9.-]{1,40}$'),
    model           text NOT NULL CHECK (model ~ '^[A-Za-z0-9._:/-]{1,200}$'),
    dims            int NOT NULL CHECK (dims BETWEEN 16 AND 16000),
    text_sha256     text NOT NULL CHECK (text_sha256 ~ '^[a-f0-9]{64}$'),
    -- NULL: the scenario's text has nothing to compare.
    embedding       vector CHECK (embedding IS NULL OR vector_dims(embedding) = dims),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (scenario_id, organization_id, project_id)
        REFERENCES scenario (id, organization_id, project_id) ON DELETE CASCADE
);
-- Matching compares a query with every scenario of one project and model
-- (exact search: a project's library is small enough; an ANN index is an
-- upgrade for when it is not, ADR-0001).
CREATE INDEX scenario_embedding_project_idx ON scenario_embedding (project_id, model);
