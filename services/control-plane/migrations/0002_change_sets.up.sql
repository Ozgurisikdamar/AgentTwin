-- A change set records what changed between two versions of one agent
-- (spec §21, §118): the computed items, the blast-radius seeds they make and
-- what the author declared. It is evidence, so it is immutable once stored.

-- Composite keys so a change set's agent belongs to its project and both of
-- its versions belong to its agent: the invariants the use case checks are
-- also the schema's.
ALTER TABLE agent ADD CONSTRAINT agent_scope_key UNIQUE (organization_id, project_id, id);
ALTER TABLE agent_version ADD CONSTRAINT agent_version_agent_key UNIQUE (agent_id, id);

CREATE TABLE change_set (
    id                   uuid PRIMARY KEY,
    organization_id      uuid NOT NULL,
    project_id           uuid NOT NULL,
    agent_id             uuid NOT NULL,
    base_version_id      uuid NOT NULL,
    candidate_version_id uuid NOT NULL,
    title                text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
    git                  jsonb,
    declared             jsonb NOT NULL DEFAULT '[]',
    items                jsonb NOT NULL,
    seeds                jsonb NOT NULL,
    scope                jsonb NOT NULL,
    summary              jsonb NOT NULL,
    -- The hash of the request: the same two versions with the same title,
    -- git and declared changes are the same change set (a re-run CI job).
    content_sha256       text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    created_by           text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CHECK (base_version_id <> candidate_version_id),
    UNIQUE (agent_id, content_sha256),
    FOREIGN KEY (organization_id, project_id, agent_id) REFERENCES agent(organization_id, project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id, base_version_id) REFERENCES agent_version(agent_id, id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id, candidate_version_id) REFERENCES agent_version(agent_id, id) ON DELETE CASCADE
);
CREATE INDEX change_set_page ON change_set (organization_id, project_id, created_at DESC, id DESC);

-- Deleting a project, agent or version still removes its change sets.
CREATE TRIGGER change_set_immutable BEFORE UPDATE ON change_set
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
