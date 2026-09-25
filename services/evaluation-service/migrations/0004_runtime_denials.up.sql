-- Actions the runtime gateway refused on a trace (policy.violation_detected.v1,
-- ADR-0033): a denial by a policy, or an approval token that did not fit the
-- action. The miner adds them to the trace as `policy_denied:<tool>`, so a
-- runtime denial is a regression signal even when the agent did not record
-- the decision itself. Kept until the trace is mined, like a flag.
CREATE TABLE regression_runtime_denial (
    decision_id uuid PRIMARY KEY,
    project_id  uuid NOT NULL,
    trace_id    text NOT NULL CHECK (trace_id ~ '^[a-f0-9]{32}$'),
    tool        text NOT NULL CHECK (tool <> ''),
    rule        text NOT NULL,
    outcome     text,
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX regression_runtime_denial_trace_idx ON regression_runtime_denial (project_id, trace_id, created_at);
