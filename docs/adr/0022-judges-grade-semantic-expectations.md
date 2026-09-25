# ADR-0022 — Judges grade semantic expectations; a judge that cannot answer is an error

* Status: accepted · Date: 2026-09-25

## Context
Deterministic expectations (state, tool calls, output text) cover most of what
matters in an agent release, but not all of it: "the reply says when the money
arrives" or "the reply tells the customer a specialist must approve" needs a
reader. Phase 2 left `semantic` expectations `SKIPPED` because no judge was
attached. Phase 3 grades them in the evaluation service, for both sides of a
baseline/candidate comparison, and the grades feed the release gate later
(Phase 5). A language-model judge is useful and fallible: it can be down, slow,
rate limited, refuse, return something that is not a verdict, invent a quote,
or simply disagree with people. Each of those must be visible, and none may
turn into a number that looks like a grade.

## Decision
**One provider interface, three implementations.** `JudgeProvider.judge(request)`
returns a structured verdict or raises `JudgeError(kind)`, with the kinds
`unavailable`, `rate_limited`, `timeout`, `rejected`, `malformed` and `refused`.

* `anthropic` — the Messages API; the verdict is the input of a *forced*
  `record_verdict` tool call, so the model cannot answer in prose.
* `openai` — any OpenAI-compatible chat-completions endpoint
  (`JUDGE_BASE_URL`), with a strict `json_schema` response format and a seed.
* `deterministic-fake` — a keyword-overlap judge for tests and the demo. It is
  labeled as what it is in every verdict ("not a language model") and is
  calibrated like any other judge.

**The verdict is validated on our side.** Providers get the verdict schema
without value constraints (not every structured-output mode supports them);
the full schema (score and confidence in [0, 1], label `pass`/`fail`, a reason,
at most 8 evidence items) is checked on receipt. Each evidence item must cite
material the judge was shown (`answer`, `customer_message`, `reference` or
`tool_call:<seq>`) and its quote must occur in that material after Unicode
normalization; other quotes are dropped and counted (`unsupported_quotes`), so a
judge that invents quotes shows. A verdict cut off by the output token limit is
`malformed`, not a partial grade.

**The prompt is versioned and hashed.** `judge-prompt/1` and the SHA-256 of the
prompt, the criteria and the schema are recorded with every verdict. Untrusted
text (the customer message, the answer, tool responses) is placed in delimited
data blocks whose tags cannot be forged from inside them.

**Grading.** A semantic expectation passes only when the label is `pass` *and*
the score reaches its threshold (default 0.7, inclusive); otherwise it fails
with the label `SEMANTIC_FAIL`. An agent that gave no answer fails without a
judge call — every criterion grades the reply. A judge error or timeout is
`ERROR` with the label `EVALUATION_ERROR` and no score, so the case it belongs
to is never a pass. An expectation that names another judge is `SKIPPED`.

**Retries, budget and cache.** Adapters retry transport errors and HTTP 408,
409, 429, 5xx and 529 with exponential backoff or the provider's
`retry-after` (capped at 10 s); other statuses are `rejected` at once. Each run
has a judge budget (cost and calls); past it, semantic expectations are
`SKIPPED` with the amount spent — deterministic expectations are never skipped
for cost. A call without a known cost counts against the call limit and is
reported. Verdicts are cached by everything that shapes them (the judge, the
prompt version and hash, the criterion, the rubric, the material shown and how
many tool calls were left out); a hit is reused as is and costs nothing.
Errors are not cached.

**Calibration.** A judge is run over examples a person labeled; raw agreement,
Cohen's kappa and the confusion matrix are reported, and a judge error counts
as a disagreement. A judge is *calibrated* for a criterion with at least 20
examples, agreement of at least 80% and kappa of at least 0.6 (undefined kappa —
every example given one label — is not calibrated). Whether the configured
judge is calibrated is recorded with every semantic result.

## Consequences
* A provider outage makes semantic expectations `ERROR`, which makes the case
  `ERRORED` and the comparison `INCOMPLETE` — the gate will see "not verified",
  never "passed".
* Only a calibrated judge's failure is meant to block a release on its own
  (Phase 5); an uncalibrated judge still grades and says so.
* The fake judge keeps the demo and every test deterministic and free; it is
  never presented as a model.
* Provider wire formats are tested by replaying their documented request and
  response shapes (`httpx.MockTransport`); a live provider is optional and
  only used when configured.

## Addendum (2026-09-25): which calibration counts, and human review
* **Which calibration counts.** Calibrations are kept per project and
  criterion. The judge's verdicts on a criterion count as calibrated when the
  *latest completed* calibration of that criterion measured *this* judge (same
  provider, model and prompt hash) and met the thresholds. So a newer
  calibration that falls short undoes an older one, and a calibration of
  another model says nothing about this one. A calibration keeps a lease while
  the judge works; one whose worker lost the lease on every attempt fails with
  that reason instead of staying `RUNNING`.
* **Which cases are judged.** A case is judged when the simulation evaluated
  it — `PASSED`, `FAILED`, or `ERRORED` — and the agent actually ran. A
  critical semantic expectation the simulation skipped makes the case
  `ERRORED`; the judge is there to grade it, and the verdict is recomputed from
  the merged results. When the agent could not be run (the `agentRun`
  finding), nothing is judged: grading a reply the agent never gave would turn
  an infrastructure problem into a verdict.
* **Human review** (spec §16.5). A person's `PASS`/`FAIL`, with a mandatory
  note, replaces one expectation's result on one side. The case and the run's
  summary are then classified again from the stored snapshots, without the
  simulation service. Reviews are append-only (the latest counts) and each is
  audited. The simulation's `agentRun` finding is not an expectation and cannot
  be overridden (`409 EXPECTATION_NOT_REVIEWABLE`). The review queue holds
  semantic results the judge could not grade (`ERROR`, `SKIPPED`), and
  critical ones it graded while uncalibrated for their criterion, until
  someone reviews them.
