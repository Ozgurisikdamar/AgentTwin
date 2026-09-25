DROP TABLE IF EXISTS change_set;
ALTER TABLE agent_version DROP CONSTRAINT IF EXISTS agent_version_agent_key;
ALTER TABLE agent DROP CONSTRAINT IF EXISTS agent_scope_key;
