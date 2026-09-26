-- When each declaration (an imported tool catalog, a scenario, a policy) was
-- made, by source and reference. A declaration replaces the previous one of
-- the same source and reference; events can arrive out of order (a retried
-- delivery, concurrent consumers), so one older than the declaration already
-- applied describes the past and is ignored instead of replacing it.
CREATE TABLE declaration (
    organization_id  uuid NOT NULL,
    project_id       uuid NOT NULL,
    source           text NOT NULL,
    source_ref       text NOT NULL,
    declared_at      timestamptz NOT NULL,
    PRIMARY KEY (organization_id, project_id, source, source_ref)
);

-- Declarations applied before this table existed, from the evidence they left.
INSERT INTO declaration (organization_id, project_id, source, source_ref, declared_at)
SELECT e.organization_id, e.project_id, v.source, v.source_ref, max(v.last_seen_at)
FROM edge_evidence v JOIN dependency_edge e ON e.id = v.edge_id
WHERE v.source <> 'OBSERVED'
GROUP BY e.organization_id, e.project_id, v.source, v.source_ref;
