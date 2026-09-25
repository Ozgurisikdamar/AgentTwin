DROP TABLE IF EXISTS gate_override;
DROP TABLE IF EXISTS gate_decision;
DROP TABLE IF EXISTS release_evaluation;
DROP FUNCTION IF EXISTS release_evaluation_guard();
DROP TABLE IF EXISTS release;
ALTER TABLE change_set DROP CONSTRAINT IF EXISTS change_set_scope_key;
