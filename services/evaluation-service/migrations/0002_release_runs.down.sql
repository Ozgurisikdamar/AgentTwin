DROP INDEX IF EXISTS eval_run_release_evaluation_idx;
ALTER TABLE eval_run DROP COLUMN IF EXISTS max_judge_cost_usd;
ALTER TABLE eval_run DROP COLUMN IF EXISTS release_evaluation_id;
