-- The vector extension stays: other schemas may use it.
DROP TABLE scenario_embedding;
ALTER TABLE scenario DROP CONSTRAINT scenario_tenancy;
