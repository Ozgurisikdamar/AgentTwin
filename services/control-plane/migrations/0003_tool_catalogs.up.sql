-- A tool catalog is one import of an API's description of its operations
-- (an OpenAPI document, an MCP server's tool list): the entries it made,
-- what each did to the tool registry, and what was skipped and why. Each
-- import of the same source is a new revision; an import equal to the
-- latest revision stores nothing. A catalog is evidence: immutable.
CREATE TABLE tool_catalog (
    id               uuid PRIMARY KEY,
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    source           text NOT NULL CHECK (source IN ('OPENAPI', 'MCP')),
    name             text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_.-]{0,62}$'),
    revision         int NOT NULL CHECK (revision > 0),
    service          text CHECK (service IS NULL OR service ~ '^[a-z0-9][a-z0-9_.-]{0,62}$'),
    title            text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
    api_version      text NOT NULL DEFAULT '' CHECK (length(api_version) <= 100),
    spec_version     text NOT NULL DEFAULT '' CHECK (length(spec_version) <= 20),
    servers          jsonb NOT NULL DEFAULT '[]',
    options          jsonb NOT NULL,
    entries          jsonb NOT NULL,
    skipped          jsonb NOT NULL,
    warnings         jsonb NOT NULL,
    summary          jsonb NOT NULL,
    content_sha256   text NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    document_sha256  text NOT NULL CHECK (document_sha256 ~ '^[a-f0-9]{64}$'),
    created_by       text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, source, name, revision),
    FOREIGN KEY (organization_id, project_id) REFERENCES project(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX tool_catalog_page ON tool_catalog (organization_id, project_id, created_at DESC, id DESC);

-- Deleting a project still removes its catalogs.
CREATE TRIGGER tool_catalog_immutable BEFORE UPDATE ON tool_catalog
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
