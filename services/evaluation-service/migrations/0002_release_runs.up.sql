-- Runs a release asks for (spec §28): the control plane's
-- evaluation.run_requested.v1 names its release evaluation, and the run is
-- created once per release evaluation however often the event arrives. The
-- release also caps what the judge may spend on its behalf.
ALTER TABLE eval_run ADD COLUMN release_evaluation_id uuid;
ALTER TABLE eval_run ADD COLUMN max_judge_cost_usd double precision
    CHECK (max_judge_cost_usd IS NULL OR max_judge_cost_usd >= 0);
CREATE UNIQUE INDEX eval_run_release_evaluation_idx ON eval_run (organization_id, release_evaluation_id)
    WHERE release_evaluation_id IS NOT NULL;
