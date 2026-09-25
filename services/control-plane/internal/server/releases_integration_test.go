package server_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
)

// capturedEvaluation is what the evaluation service answered for the
// demo's release 1.2.4 → 1.3.0 (run with seed 42 against the real
// simulation service and demo agent): the run, every compared case and
// the judge. The candidate refunds without the policy check, claims a
// refund that failed and refunds twice after a timeout.
func capturedEvaluation(t *testing.T) map[string]any {
	t.Helper()
	raw, err := os.ReadFile("../release/testdata/demo-release-1.2.4-1.3.0.json")
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// deepCopy copies a decoded JSON value.
func deepCopy(v any) any {
	raw, _ := json.Marshal(v)
	var out any
	_ = json.Unmarshal(raw, &out)
	return out
}

// mirrored turns the captured evaluation into one where the candidate did
// exactly what the baseline did.
func mirrored(ev map[string]any) map[string]any {
	ev = deepCopy(ev).(map[string]any)
	run := ev["run"].(map[string]any)
	n := run["run"].(map[string]any)["case_count"]
	counts := map[string]any{"IMPROVED": 0, "INCOMPLETE": 0, "NEW_CRITICAL_FAILURE": 0, "REGRESSED": 0, "UNCHANGED": n}
	run["run"].(map[string]any)["counts"] = counts
	summary := run["summary"].(map[string]any)
	summary["candidate"] = deepCopy(summary["baseline"])
	summary["counts"] = counts
	for _, c := range run["cases"].([]any) {
		c := c.(map[string]any)
		c["classification"], c["reason"], c["candidate"] = "UNCHANGED", "Same result on both sides.", deepCopy(c["baseline"])
	}
	for _, c := range ev["cases"].(map[string]any) {
		c := c.(map[string]any)
		cmp := c["comparison"].(map[string]any)
		cmp["classification"], cmp["reason"], cmp["candidate"] = "UNCHANGED", "Same result on both sides.", deepCopy(cmp["baseline"])
		cmp["divergence"] = nil
		for _, e := range cmp["expectations"].([]any) {
			e := e.(map[string]any)
			e["candidate"] = e["baseline"]
			delete(e, "candidate_reason")
			if reason, ok := e["baseline_reason"]; ok {
				e["candidate_reason"] = reason
			}
		}
		res := c["results"].(map[string]any)
		res["candidate"] = deepCopy(res["baseline"])
	}
	return ev
}

// releaseFixture drives releases of the demo agent with the graph, the
// simulation and the evaluation services faked; the evaluation service
// answers from a captured evaluation.
type releaseFixture struct {
	h                       *harness
	graph, simulation, eval *fakeService
	org, pid, eng           string
	promptKey               string

	mu          sync.Mutex
	evidence    map[string]any            // what the evaluation service answers for a new run
	runs        map[string]map[string]any // eval run id → its detail
	unavailable bool                      // the evaluation service answers 503
	casesDown   bool                      // the evaluation service answers 503 for cases
	scenarios   bool                      // the graph and the library link scenarios
	libraryDown bool                      // the scenario library answers 503
}

func (fx *releaseFixture) set(f func()) {
	fx.mu.Lock()
	defer fx.mu.Unlock()
	f()
}

func notFoundBody(code string) map[string]any {
	return map[string]any{"error": map[string]any{"code": code, "message": "not found", "request_id": "fake"}}
}

// librarySuite is the library's answer: the captured evaluation's
// scenarios, each close to the changed prompt.
func librarySuite(evidence map[string]any, body map[string]any) map[string]any {
	query := ""
	if qs, _ := body["queries"].([]any); len(qs) > 0 {
		query = qs[0].(map[string]any)["id"].(string)
	}
	var scenarios []any
	for _, c := range evidence["run"].(map[string]any)["cases"].([]any) {
		c := c.(map[string]any)
		var tags []string
		for _, tg := range c["tags"].([]any) {
			tags = append(tags, tg.(string))
		}
		scenarios = append(scenarios, matchedScenario(c["scenario_name"].(string), c["severity"].(string), "manual", tags,
			false, []string{}, false, map[string]any{"query": query, "similarity": 0.5}))
	}
	return map[string]any{"project_id": body["project_id"], "agent": body["agent"], "embedding_model": "hashing-v1",
		"min_similarity": 0.25, "scenarios": scenarios, "unknown_names": []string{}, "unembedded_queries": []string{},
		"truncated": false}
}

func newReleaseFixture(t *testing.T) *releaseFixture {
	t.Helper()
	fx := &releaseFixture{evidence: capturedEvaluation(t), runs: map[string]map[string]any{}, scenarios: true}
	fx.graph = newFakeService(t, "graph-service", "openapi/graph-service.openapi.yaml", func(map[string]any) (int, any) {
		fx.mu.Lock()
		defer fx.mu.Unlock()
		out := blastRadius(fx.promptKey)
		br := out["blast_radius"].(map[string]any)
		if !fx.scenarios {
			br["scenarios"] = []any{}
		}
		return 200, out
	})
	fx.simulation = newFakeService(t, "simulation-service", "openapi/simulation-service.openapi.yaml",
		func(body map[string]any) (int, any) {
			fx.mu.Lock()
			defer fx.mu.Unlock()
			if fx.libraryDown {
				return 503, map[string]any{"error": map[string]any{"code": "UNAVAILABLE", "message": "busy", "request_id": "fake"}}
			}
			out := librarySuite(fx.evidence, body)
			if !fx.scenarios {
				out["scenarios"] = []any{}
			}
			// The graph's second scenario is no longer in the library.
			out["unknown_names"] = []string{"gone-scenario"}
			return 200, out
		})
	fx.eval = newRoutedFakeService(t, "evaluation-service", "openapi/evaluation-service.openapi.yaml",
		func(r *http.Request, _ map[string]any) (int, any) {
			fx.mu.Lock()
			defer fx.mu.Unlock()
			if fx.unavailable {
				return 503, map[string]any{"error": map[string]any{"code": "UNAVAILABLE", "message": "busy", "request_id": "fake"}}
			}
			parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/v1/"), "/")
			switch {
			case len(parts) == 1 && parts[0] == "judges":
				return 200, fx.evidence["judges"]
			case len(parts) == 2 && parts[0] == "eval-runs":
				if run, ok := fx.runs[parts[1]]; ok {
					return 200, run["run"]
				}
				return 404, notFoundBody("EVAL_RUN_NOT_FOUND")
			case len(parts) == 4 && parts[0] == "eval-runs" && parts[2] == "cases":
				if fx.casesDown {
					return 503, map[string]any{"error": map[string]any{"code": "UNAVAILABLE", "message": "busy", "request_id": "fake"}}
				}
				if run, ok := fx.runs[parts[1]]; ok {
					if c, ok := run["cases"].(map[string]any)[parts[3]]; ok {
						return 200, c
					}
				}
				return 404, notFoundBody("CASE_NOT_FOUND")
			}
			t.Errorf("evaluation service: unexpected %s %s", r.Method, r.URL.Path)
			return 404, notFoundBody("NOT_FOUND")
		})
	fx.h = newHarness(t, map[string]string{"graph-service": fx.graph.srv.URL, "simulation-service": fx.simulation.srv.URL,
		"evaluation-service": fx.eval.srv.URL})
	fx.org, fx.pid = fx.h.s.Demo.OrganizationID, fx.h.s.Demo.ProjectID
	fx.eng = fx.h.login("engineer@demo.agenttwin.dev")
	fx.h.registerDemo(fx.eng, fx.pid, "1.2.4", "1.3.0")
	cs := fx.h.createChangeSet(fx.eng, fx.pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4",
		"candidate_version": "1.3.0"})
	if cs.Status != http.StatusCreated {
		t.Fatalf("change set: %d %s", cs.Status, cs.Raw)
	}
	fx.promptKey = cs.Body["seeds"].([]any)[0].(map[string]any)["component"].(map[string]any)["key"].(string)
	return fx
}

// create creates a release of the demo's 1.3.0 against 1.2.4.
func (fx *releaseFixture) create(tok string, extra map[string]any) resp {
	fx.h.t.Helper()
	body := map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.2.4",
		"candidate_version": "1.3.0"}
	for k, v := range extra {
		body[k] = v
	}
	return fx.h.request("POST", "/api/v1/releases", body, bearer(tok))
}

func (fx *releaseFixture) gate(tok, releaseID string, revision int) resp {
	fx.h.t.Helper()
	path := "/api/v1/releases/" + releaseID + "/gate"
	if revision > 0 {
		path += fmt.Sprintf("?revision=%d", revision)
	}
	return fx.h.request("GET", path, nil, bearer(tok))
}

// requested is the evaluation.run_requested.v1 event of a release
// evaluation, validated against its schema.
func (fx *releaseFixture) requested(t *testing.T, evaluationID string) map[string]any {
	t.Helper()
	var raw []byte
	if err := fx.h.pool.QueryRow(context.Background(), `SELECT envelope::text FROM control.outbox
		WHERE event_type = 'evaluation.run_requested.v1' AND envelope->'payload'->>'release_evaluation_id' = $1`,
		evaluationID).Scan(&raw); err != nil {
		t.Fatalf("run_requested event of %s: %v", evaluationID, err)
	}
	v, err := events.DefaultValidator()
	if err != nil {
		t.Fatal(err)
	}
	env, err := v.ValidateRaw(raw)
	if err != nil {
		t.Fatalf("run_requested event: %v", err)
	}
	var payload map[string]any
	if err := json.Unmarshal(env.Payload, &payload); err != nil {
		t.Fatal(err)
	}
	return payload
}

// complete answers the evaluation of a release evaluation as the
// evaluation service would: it stores a run of the given evidence (nil:
// a run the service does not know) and delivers
// evaluation.run_completed.v1.
func (fx *releaseFixture) complete(t *testing.T, evaluationID string, evidence map[string]any) (string, error) {
	t.Helper()
	runID := ids.New()
	status := "COMPLETED"
	if evidence != nil {
		evidence = deepCopy(evidence).(map[string]any)
		run := evidence["run"].(map[string]any)["run"].(map[string]any)
		run["id"], run["release_evaluation_id"], run["organization_id"], run["project_id"] = runID, evaluationID, fx.org, fx.pid
		status = run["status"].(string)
		for _, c := range evidence["cases"].(map[string]any) {
			c.(map[string]any)["eval_run_id"] = runID
		}
		fx.set(func() { fx.runs[runID] = evidence })
	}
	return runID, fx.deliver(t, map[string]any{"eval_run_id": runID, "release_evaluation_id": evaluationID, "status": status,
		"error": nil})
}

// deliver hands the control plane an evaluation.run_completed.v1 payload.
func (fx *releaseFixture) deliver(t *testing.T, payload map[string]any) error {
	t.Helper()
	env, err := events.New("evaluation.run_completed.v1", "evaluation-service", fx.org, fx.pid, "", "", payload)
	if err != nil {
		t.Fatal(err)
	}
	v, _ := events.DefaultValidator()
	if err := v.ValidatePayload(env); err != nil {
		t.Fatalf("run_completed payload: %v", err)
	}
	return fx.h.s.App.HandleEvaluationCompleted(context.Background(), env)
}

func ruleNames(t *testing.T, g map[string]any) []string {
	t.Helper()
	var out []string
	for _, r := range g["decision"].(map[string]any)["rules"].([]any) {
		out = append(out, r.(map[string]any)["rule"].(string))
	}
	sort.Strings(out)
	return out
}

func gateEvaluationID(t *testing.T, r resp) string {
	t.Helper()
	g := r.Body
	if gate, ok := r.Body["gate"].(map[string]any); ok {
		g = gate
	}
	id, _ := g["release_evaluation_id"].(string)
	if id == "" {
		t.Fatalf("no release evaluation in %s", r.Raw)
	}
	return id
}

// The acceptance of Phase 5: the demo's 1.3.0 is a bad candidate, and the
// gate blocks it with the evidence of why; the decision is stored with
// what it rests on and cannot be changed.
func TestABadCandidateIsBlocked(t *testing.T) {
	fx := newReleaseFixture(t)
	h := fx.h

	created := fx.create(fx.eng, map[string]any{"title": "Refund flow rewrite",
		"git":    map[string]any{"base_commit": "0a1b2c3d", "candidate_commit": "4e5f6a7b", "changed_files": []string{"agent/prompt.md"}},
		"ci_url": "https://ci.example.com/runs/42"})
	if created.Status != http.StatusCreated {
		t.Fatalf("create: %d %s", created.Status, created.Raw)
	}
	rel := created.Body["release"].(map[string]any)
	releaseID := rel["id"].(string)
	if rel["commit_sha"] != "4e5f6a7b" || rel["ci_url"] != "https://ci.example.com/runs/42" || rel["title"] != "Refund flow rewrite" ||
		rel["agent"].(map[string]any)["name"] != "support-refund-agent" || rel["baseline"].(map[string]any)["version"] != "1.2.4" ||
		rel["candidate"].(map[string]any)["version"] != "1.3.0" {
		t.Fatalf("release = %s", created.Raw)
	}
	gate := created.Body["gate"].(map[string]any)
	if gate["status"] != "EVALUATING" || gate["effective_outcome"] != "PENDING" || gate["exit_code"] != nil ||
		gate["decision"] != nil || gate["revision"] != 1.0 {
		t.Fatalf("gate before the run = %s", created.Raw)
	}
	if rg := rel["gate"].(map[string]any); rg["effective_outcome"] != "PENDING" || rg["revision"] != 1.0 {
		t.Fatalf("release gate summary = %v", rg)
	}
	evalID := gateEvaluationID(t, created)

	// The suite: the captured run's nine scenarios, each pinned to the
	// version the impact selected.
	suite := gate["suite"].([]any)
	if len(suite) != 9 {
		t.Fatalf("suite = %d scenarios: %v", len(suite), suite)
	}
	for _, e := range suite {
		e := e.(map[string]any)
		if !ids.Valid(fmt.Sprint(e["scenario_version_id"])) || len(e["why"].([]any)) == 0 {
			t.Errorf("suite entry = %v", e)
		}
	}

	// The evaluation service is asked to run exactly that suite, pinned,
	// with the release's seed and the policy's budget.
	req := fx.requested(t, evalID)
	if req["release_id"] != releaseID || req["baseline"].(map[string]any)["version"] != "1.2.4" ||
		req["candidate"].(map[string]any)["version"] != "1.3.0" || len(req["suite"].([]any)) != 9 ||
		req["requested_by"] == "" || req["budget"].(map[string]any)["max_gate_cost_usd"] == nil {
		t.Fatalf("run requested = %v", req)
	}
	for i, e := range req["suite"].([]any) {
		if e.(map[string]any)["scenario_version_id"] != suite[i].(map[string]any)["scenario_version_id"] {
			t.Errorf("event suite[%d] = %v, gate suite = %v", i, e, suite[i])
		}
	}
	seed := req["seed"]

	// Pending: CI must not pass yet.
	if r := fx.gate(fx.eng, releaseID, 0); r.Status != 200 || r.Body["effective_outcome"] != "PENDING" || r.Body["ci_fails"] != nil {
		t.Fatalf("pending gate: %d %s", r.Status, r.Raw)
	}

	// The evaluation service reports the run.
	runID, err := fx.complete(t, evalID, fx.evidence)
	if err != nil {
		t.Fatalf("run completed: %v", err)
	}
	r := fx.gate(fx.eng, releaseID, 0)
	if r.Status != 200 {
		t.Fatalf("gate: %d %s", r.Status, r.Raw)
	}
	g := r.Body
	decision := g["decision"].(map[string]any)
	if g["status"] != "DECIDED" || g["effective_outcome"] != "BLOCK" || decision["outcome"] != "BLOCK" ||
		g["exit_code"] != 3.0 || g["ci_fails"] != true || g["eval_run_id"] != runID || decision["incomplete"] != false {
		t.Fatalf("decided gate = %s", r.Raw)
	}
	want := []string{"cost_regression", "critical_failure", "duplicate_side_effect", "policy_violation", "regression",
		"semantic_regression", "unverified_success"}
	if got := ruleNames(t, g); !slices.Equal(got, want) {
		t.Errorf("rules = %v, want %v", got, want)
	}
	counts := decision["counts"].(map[string]any)
	if counts["required"] != 9.0 || counts["evaluated"] != 9.0 || counts["new_critical_failures"] != 2.0 ||
		counts["regressed"] != 1.0 || counts["failed"] != 3.0 {
		t.Errorf("counts = %v", counts)
	}
	// Why it is blocked, in the decision itself: the failing scenarios and
	// their traces.
	var critical map[string]any
	for _, rule := range decision["rules"].([]any) {
		if rule.(map[string]any)["rule"] == "critical_failure" {
			critical = rule.(map[string]any)
		}
	}
	if critical == nil || critical["outcome"] != "BLOCK" || len(critical["evidence"].([]any)) == 0 {
		t.Fatalf("critical_failure = %v", critical)
	}
	for _, ev := range critical["evidence"].([]any) {
		ev := ev.(map[string]any)
		if ev["scenario"] == "" || ev["trace_id"] == nil || ev["summary"] == "" {
			t.Errorf("evidence = %v", ev)
		}
	}
	summary := g["summary"].(map[string]any)
	if summary["scenarios"] != 9.0 || summary["evaluated"] != 9.0 || summary["new_critical_failures"] != 2.0 ||
		summary["eval_run_status"] != "COMPLETED" || summary["cost"] == nil || summary["cost_delta_usd"] == nil ||
		summary["exit_code"] != 3.0 || fmt.Sprintf("%.2f", summary["latency_p95_delta_ms"]) != "-16.00" {
		t.Errorf("summary = %v", summary)
	}
	if !strings.HasPrefix(fmt.Sprint(g["evidence_sha256"]), "") || len(fmt.Sprint(g["evidence_sha256"])) != 64 ||
		g["evidence_verified"] != true {
		t.Errorf("evidence = %v %v", g["evidence_sha256"], g["evidence_verified"])
	}

	// The control plane read the run on behalf of the release's project only.
	callers, _ := fx.eval.calls()
	if len(callers) < 11 {
		t.Errorf("evaluation service calls = %d, want the run, 9 cases and the judge", len(callers))
	}
	for _, p := range callers {
		if p.OrgID != fx.org || !slices.Equal(p.ProjectIDs, []string{fx.pid}) {
			t.Errorf("evaluation service asked by %+v", p)
		}
	}

	// A redelivered event changes nothing, and asks nothing again.
	before := len(callers)
	if err := fx.deliver(t, map[string]any{"eval_run_id": runID, "release_evaluation_id": evalID, "status": "COMPLETED"}); err != nil {
		t.Fatal(err)
	}
	if n := h.count(`SELECT count(*) FROM control.gate_decision WHERE release_id = $1`, releaseID); n != 1 {
		t.Fatalf("decisions = %d, want 1", n)
	}
	if after, _ := fx.eval.calls(); len(after) != before {
		t.Errorf("a redelivered event read the run again: %d → %d calls", before, len(after))
	}
	if again := fx.gate(fx.eng, releaseID, 0); again.Body["evidence_sha256"] != g["evidence_sha256"] {
		t.Fatalf("the decision changed: %s", again.Raw)
	}

	// The release shows its gate, in the list and in the detail.
	detail := h.request("GET", "/api/v1/releases/"+releaseID, nil, bearer(h.login("viewer@demo.agenttwin.dev")))
	if detail.Status != 200 {
		t.Fatalf("detail: %d %s", detail.Status, detail.Raw)
	}
	revs := detail.Body["revisions"].([]any)
	if len(revs) != 1 || revs[0].(map[string]any)["effective_outcome"] != "BLOCK" || revs[0].(map[string]any)["risk_index"] == nil {
		t.Fatalf("revisions = %v", revs)
	}
	list := h.request("GET", "/api/v1/releases?project_id="+fx.pid, nil, bearer(fx.eng))
	if list.Status != 200 || len(list.Body["items"].([]any)) != 1 ||
		list.Body["items"].([]any)[0].(map[string]any)["gate"].(map[string]any)["outcome"] != "BLOCK" {
		t.Fatalf("list: %d %s", list.Status, list.Raw)
	}

	// Evidence is immutable (spec §91): the database refuses to change a
	// release, what an evaluation rests on, or a decision.
	ctx := context.Background()
	for _, stmt := range []string{
		`UPDATE control.release SET title = 'changed' WHERE id = $1`,
		`UPDATE control.gate_decision SET outcome = 'PASS' WHERE release_id = $1`,
		`UPDATE control.gate_decision SET decision = '{}' WHERE release_id = $1`,
		`UPDATE control.release_evaluation SET suite = '[]' WHERE release_id = $1`,
		`UPDATE control.release_evaluation SET status = 'EVALUATING', decided_at = NULL WHERE release_id = $1`,
		`UPDATE control.release_evaluation SET eval_run_id = gen_random_uuid() WHERE release_id = $1`,
	} {
		if _, err := h.pool.Exec(ctx, stmt, releaseID); err == nil {
			t.Errorf("%s: allowed", stmt)
		}
	}

	// A rerun is a new revision with the same seed; the first decision stays.
	rerun := h.request("POST", "/api/v1/releases/"+releaseID+"/evaluate", nil, bearer(fx.eng))
	if rerun.Status != http.StatusAccepted || rerun.Body["revision"] != 2.0 || rerun.Body["effective_outcome"] != "PENDING" {
		t.Fatalf("rerun: %d %s", rerun.Status, rerun.Raw)
	}
	if again := fx.requested(t, gateEvaluationID(t, rerun)); again["seed"] != seed {
		t.Errorf("rerun seed = %v, want %v", again["seed"], seed)
	}
	if first := fx.gate(fx.eng, releaseID, 1); first.Body["effective_outcome"] != "BLOCK" || first.Body["evidence_sha256"] != g["evidence_sha256"] {
		t.Fatalf("revision 1 = %s", first.Raw)
	}

	// Every step is audited.
	for _, action := range []string{"release.created", "release.evaluate", "release.gate_decided"} {
		if n := h.count(`SELECT count(*) FROM control.audit_event WHERE action = $1 AND resource_id = $2`, action, releaseID); n == 0 {
			t.Errorf("no audit entry %s", action)
		}
	}
}

// Tampering with stored evidence behind the database's back is detected
// when the gate is read.
func TestTamperedEvidenceIsReported(t *testing.T) {
	fx := newReleaseFixture(t)
	created := fx.create(fx.eng, nil)
	releaseID := created.Body["release"].(map[string]any)["id"].(string)
	if _, err := fx.complete(t, gateEvaluationID(t, created), fx.evidence); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	for _, stmt := range []string{
		`ALTER TABLE control.gate_decision DISABLE TRIGGER gate_decision_immutable`,
		`UPDATE control.gate_decision SET decision = jsonb_set(decision, '{outcome}', '"PASS"') WHERE release_id = $1`,
		`ALTER TABLE control.gate_decision ENABLE TRIGGER gate_decision_immutable`,
	} {
		var args []any
		if strings.Contains(stmt, "$1") {
			args = append(args, releaseID)
		}
		if _, err := fx.h.pool.Exec(ctx, stmt, args...); err != nil {
			t.Fatalf("%s: %v", stmt, err)
		}
	}
	if r := fx.gate(fx.eng, releaseID, 0); r.Status != 200 || r.Body["evidence_verified"] != false {
		t.Fatalf("tampered gate: %d %s", r.Status, r.Raw)
	}
}

// An override lets a gated release through without rewriting history
// (spec §92).
func TestAnOverrideNeverRewritesTheDecision(t *testing.T) {
	fx := newReleaseFixture(t)
	h := fx.h
	owner := h.login("owner@demo.agenttwin.dev")
	admin := h.login("admin@demo.agenttwin.dev")
	reviewer := h.login("reviewer@demo.agenttwin.dev")
	viewer := h.login("viewer@demo.agenttwin.dev")

	created := fx.create(fx.eng, nil)
	releaseID := created.Body["release"].(map[string]any)["id"].(string)
	override := func(tok string, body map[string]any) resp {
		return h.request("POST", "/api/v1/releases/"+releaseID+"/override", body, bearer(tok))
	}
	reason := map[string]any{"reason": "Hotfix for the outage, reviewed by the refunds team."}

	// Nothing decided yet: nothing to override.
	if r := override(reviewer, reason); r.Status != 409 || errCode(r) != "GATE_PENDING" {
		t.Fatalf("pending: %d %s", r.Status, r.Raw)
	}
	if _, err := fx.complete(t, gateEvaluationID(t, created), fx.evidence); err != nil {
		t.Fatal(err)
	}
	blocked := fx.gate(fx.eng, releaseID, 0).Body

	future := time.Now().Add(24 * time.Hour).UTC().Format(time.RFC3339)
	for _, c := range []struct {
		name, tok string
		body      map[string]any
		status    int
		code      string
	}{
		{"engineer lacks the permission", fx.eng, reason, 403, "FORBIDDEN"},
		{"viewer lacks the permission", viewer, reason, 403, "FORBIDDEN"},
		{"no reason", reviewer, map[string]any{"reason": "   ok     "}, 400, "INVALID_OVERRIDE"},
		{"a reason with control characters", reviewer, map[string]any{"reason": "Because\u0007 it is fine."}, 400, "INVALID_OVERRIDE"},
		{"ticket is not a URL", reviewer, map[string]any{"reason": reason["reason"], "ticket_url": "javascript:alert(1)"}, 400, "INVALID_OVERRIDE"},
		{"expired already", reviewer, map[string]any{"reason": reason["reason"], "expires_at": "2020-01-01T00:00:00Z"}, 400, "INVALID_OVERRIDE"},
		{"too far away", reviewer, map[string]any{"reason": reason["reason"], "expires_at": time.Now().Add(91 * 24 * time.Hour).UTC().Format(time.RFC3339)}, 400, "INVALID_OVERRIDE"},
		{"not the latest revision", reviewer, map[string]any{"reason": reason["reason"], "revision": 2}, 409, "NOT_LATEST_REVISION"},
		{"unknown field", reviewer, map[string]any{"reason": reason["reason"], "outcome": "PASS"}, 400, "INVALID_JSON"},
		{"negative revision", reviewer, map[string]any{"reason": reason["reason"], "revision": -1}, 400, "INVALID_OVERRIDE"},
	} {
		t.Run(c.name, func(t *testing.T) {
			if r := override(c.tok, c.body); r.Status != c.status || errCode(r) != c.code {
				t.Fatalf("%d %s, want %d %s", r.Status, r.Raw, c.status, c.code)
			}
		})
	}

	// A reviewer may override when the project's policy allows it (the default).
	r := override(reviewer, map[string]any{"reason": reason["reason"], "ticket_url": "https://tickets.example.com/OPS-12",
		"expires_at": future, "revision": 1})
	if r.Status != http.StatusCreated {
		t.Fatalf("override: %d %s", r.Status, r.Raw)
	}
	g := r.Body
	o := g["override"].(map[string]any)
	if g["effective_outcome"] != "OVERRIDDEN" || g["exit_code"] != 0.0 || g["ci_fails"] != false ||
		g["decision"].(map[string]any)["outcome"] != "BLOCK" || o["original_outcome"] != "BLOCK" || o["active"] != true ||
		o["actor"] == "" || o["reason"] != reason["reason"] || o["ticket_url"] != "https://tickets.example.com/OPS-12" {
		t.Fatalf("overridden gate = %s", r.Raw)
	}
	if g["evidence_sha256"] != blocked["evidence_sha256"] || g["evidence_verified"] != true {
		t.Fatal("the override changed the decision's evidence")
	}
	if n := h.count(`SELECT count(*) FROM control.audit_event WHERE action = 'release.override' AND reason = $1`, reason["reason"]); n != 1 {
		t.Errorf("override audit entries = %d", n)
	}
	detail := h.request("GET", "/api/v1/releases/"+releaseID, nil, bearer(viewer)).Body
	rev := detail["revisions"].([]any)[0].(map[string]any)
	if rev["outcome"] != "BLOCK" || rev["effective_outcome"] != "OVERRIDDEN" || rev["overridden"] != true {
		t.Fatalf("revision = %v", rev)
	}

	// One override per decision.
	if r := override(admin, reason); r.Status != 409 || errCode(r) != "ALREADY_OVERRIDDEN" {
		t.Fatalf("second override: %d %s", r.Status, r.Raw)
	}
	if _, err := h.pool.Exec(context.Background(), `UPDATE control.gate_override SET reason = 'rewritten reason' WHERE release_id = $1`, releaseID); err == nil {
		t.Error("an override can be rewritten")
	}

	// An expired override no longer applies: the gate blocks again, and
	// still says it was overridden.
	h.s.App.Now = func() time.Time { return time.Now().Add(48 * time.Hour) }
	expired := fx.gate(fx.eng, releaseID, 0).Body
	if expired["effective_outcome"] != "BLOCK" || expired["exit_code"] != 3.0 || expired["override"].(map[string]any)["active"] != false {
		t.Fatalf("expired override: %v", expired)
	}
	h.s.App.Now = time.Now

	// A new evaluation is gated anew; its predecessor's override stays with it.
	rerun := h.request("POST", "/api/v1/releases/"+releaseID+"/evaluate", nil, bearer(fx.eng))
	if rerun.Status != http.StatusAccepted || rerun.Body["override"] != nil {
		t.Fatalf("rerun: %d %s", rerun.Status, rerun.Raw)
	}
	if r := override(owner, map[string]any{"reason": reason["reason"], "revision": 1}); r.Status != 409 || errCode(r) != "NOT_LATEST_REVISION" {
		t.Fatalf("override of revision 1: %d %s", r.Status, r.Raw)
	}
	if _, err := fx.complete(t, gateEvaluationID(t, rerun), fx.evidence); err != nil {
		t.Fatal(err)
	}
	if g := fx.gate(fx.eng, releaseID, 0).Body; g["effective_outcome"] != "BLOCK" || g["override"] != nil {
		t.Fatalf("revision 2 = %v", g)
	}

	// When the policy keeps overrides to owners and administrators, a
	// reviewer is refused and an administrator is not.
	if r := h.request("PATCH", "/api/v1/projects/"+fx.pid, map[string]any{"gate_policy": map[string]any{"allowReviewerOverride": false}},
		bearer(owner)); r.Status != 200 {
		t.Fatalf("policy: %d %s", r.Status, r.Raw)
	}
	if r := override(reviewer, reason); r.Status != 403 || errCode(r) != "OVERRIDE_NOT_ALLOWED" {
		t.Fatalf("reviewer under a strict policy: %d %s", r.Status, r.Raw)
	}
	multiline := "Signed off by the refunds team.\n\tSee the incident review."
	if r := override(admin, map[string]any{"reason": multiline}); r.Status != http.StatusCreated ||
		r.Body["override"].(map[string]any)["expires_at"] != nil || r.Body["override"].(map[string]any)["reason"] != multiline {
		t.Fatalf("administrator: %d %s", r.Status, r.Raw)
	}
}

// Missing evidence is never a pass: a run the evaluation service does not
// know, a run of another evaluation or a failed run blocks as incomplete;
// a service that cannot answer now is retried.
func TestMissingEvidenceBlocks(t *testing.T) {
	fx := newReleaseFixture(t)

	evaluate := func() (string, string) {
		created := fx.create(fx.eng, nil)
		if created.Status != http.StatusCreated {
			t.Fatalf("create: %d %s", created.Status, created.Raw)
		}
		return created.Body["release"].(map[string]any)["id"].(string), gateEvaluationID(t, created)
	}
	incomplete := func(t *testing.T, releaseID, contains string) {
		t.Helper()
		g := fx.gate(fx.eng, releaseID, 0).Body
		d, _ := g["decision"].(map[string]any)
		if d == nil || d["outcome"] != "BLOCK" || d["incomplete"] != true || g["exit_code"] != 3.0 {
			t.Fatalf("gate = %v", g)
		}
		if !slices.Contains(ruleNames(t, g), "incomplete") || !strings.Contains(fmt.Sprint(d["rules"]), contains) {
			t.Fatalf("rules = %v, want incomplete mentioning %q", d["rules"], contains)
		}
	}

	t.Run("the service does not know the run", func(t *testing.T) {
		releaseID, evalID := evaluate()
		if _, err := fx.complete(t, evalID, nil); err != nil {
			t.Fatal(err)
		}
		incomplete(t, releaseID, "not known to the evaluation service")
	})

	t.Run("the run is another evaluation's", func(t *testing.T) {
		releaseID, evalID := evaluate()
		_, other := evaluate()
		runID, err := fx.complete(t, other, fx.evidence)
		if err != nil {
			t.Fatal(err)
		}
		if err := fx.deliver(t, map[string]any{"eval_run_id": runID, "release_evaluation_id": evalID, "status": "COMPLETED"}); err != nil {
			t.Fatal(err)
		}
		incomplete(t, releaseID, "was not run for this release evaluation")
	})

	t.Run("the run failed", func(t *testing.T) {
		releaseID, evalID := evaluate()
		failed := deepCopy(fx.evidence).(map[string]any)
		run := failed["run"].(map[string]any)["run"].(map[string]any)
		run["status"], run["error"] = "FAILED", "The candidate's endpoint did not answer."
		before, _ := fx.eval.calls()
		if _, err := fx.complete(t, evalID, failed); err != nil {
			t.Fatal(err)
		}
		incomplete(t, releaseID, "did not answer")
		// The cases of a run that did not complete are not evidence: only
		// the run and the judge were read.
		if after, _ := fx.eval.calls(); len(after)-len(before) != 2 {
			t.Errorf("calls for a failed run = %d, want 2", len(after)-len(before))
		}
	})

	t.Run("a case the service no longer has", func(t *testing.T) {
		// The decision is still made; the case keeps its statuses.
		releaseID, evalID := evaluate()
		partial := deepCopy(fx.evidence).(map[string]any)
		delete(partial["cases"].(map[string]any), "refund-happy-path")
		if _, err := fx.complete(t, evalID, partial); err != nil {
			t.Fatal(err)
		}
		if g := fx.gate(fx.eng, releaseID, 0).Body; g["effective_outcome"] != "BLOCK" ||
			g["decision"].(map[string]any)["counts"].(map[string]any)["evaluated"] != 9.0 {
			t.Fatalf("gate = %v", g)
		}
	})

	t.Run("the service is not configured", func(t *testing.T) {
		releaseID, evalID := evaluate()
		client := fx.h.s.App.Evaluation
		fx.h.s.App.Evaluation = nil
		defer func() { fx.h.s.App.Evaluation = client }()
		if _, err := fx.complete(t, evalID, fx.evidence); err != nil {
			t.Fatal(err)
		}
		incomplete(t, releaseID, "not configured")
	})

	t.Run("the service cannot answer now", func(t *testing.T) {
		releaseID, evalID := evaluate()
		fx.set(func() { fx.unavailable = true })
		runID, err := fx.complete(t, evalID, fx.evidence)
		if err == nil || events.IsPermanent(err) {
			t.Fatalf("delivery = %v, want a retryable error", err)
		}
		if g := fx.gate(fx.eng, releaseID, 0).Body; g["effective_outcome"] != "PENDING" {
			t.Fatalf("gate after a failed read = %v", g)
		}
		fx.set(func() { fx.unavailable, fx.casesDown = false, true })
		if err := fx.deliver(t, map[string]any{"eval_run_id": runID, "release_evaluation_id": evalID, "status": "COMPLETED"}); err == nil {
			t.Fatal("a case the service could not answer was ignored")
		}
		fx.set(func() { fx.casesDown = false })
		if err := fx.deliver(t, map[string]any{"eval_run_id": runID, "release_evaluation_id": evalID, "status": "COMPLETED"}); err != nil {
			t.Fatal(err)
		}
		if g := fx.gate(fx.eng, releaseID, 0).Body; g["effective_outcome"] != "BLOCK" || g["decision"].(map[string]any)["incomplete"] != false {
			t.Fatalf("gate after the retry = %v", g)
		}
	})

	t.Run("events that are not the gate's", func(t *testing.T) {
		// A run nobody's release asked for, and one of an evaluation that
		// does not exist: acknowledged, nothing stored.
		before := fx.h.count(`SELECT count(*) FROM control.gate_decision`)
		for _, p := range []map[string]any{
			{"eval_run_id": ids.New(), "release_evaluation_id": nil, "status": "COMPLETED"},
			{"eval_run_id": ids.New(), "release_evaluation_id": "", "status": "COMPLETED"},
			{"eval_run_id": ids.New(), "status": "COMPLETED"},
			{"eval_run_id": ids.New(), "release_evaluation_id": ids.New(), "status": "COMPLETED"},
		} {
			if err := fx.deliver(t, p); err != nil {
				t.Fatalf("%v: %v", p, err)
			}
		}
		// The schema leaves release_evaluation_id free-form; the gate does not.
		if err := fx.deliver(t, map[string]any{"eval_run_id": ids.New(), "release_evaluation_id": "not-a-uuid", "status": "COMPLETED"}); !events.IsPermanent(err) {
			t.Fatalf("malformed ids: %v, want a permanent error", err)
		}
		if after := fx.h.count(`SELECT count(*) FROM control.gate_decision`); after != before {
			t.Fatalf("decisions %d → %d", before, after)
		}
	})
}

// A candidate that behaves like its baseline passes; a suite with nothing
// to run is decided at once.
func TestAGoodCandidatePassesAndAnEmptySuiteWarns(t *testing.T) {
	fx := newReleaseFixture(t)
	h := fx.h

	created := fx.create(fx.eng, nil)
	releaseID := created.Body["release"].(map[string]any)["id"].(string)
	if _, err := fx.complete(t, gateEvaluationID(t, created), mirrored(fx.evidence)); err != nil {
		t.Fatal(err)
	}
	g := fx.gate(fx.eng, releaseID, 0).Body
	if g["effective_outcome"] != "PASS" || g["exit_code"] != 0.0 || g["ci_fails"] != false {
		t.Fatalf("mirrored candidate = %v (rules %v)", g["effective_outcome"], g["decision"].(map[string]any)["rules"])
	}
	if s := g["summary"].(map[string]any); s["cost_delta_usd"] != 0.0 || s["latency_p95_delta_ms"] != 0.0 {
		t.Errorf("summary = %v", s)
	}
	if r := h.request("POST", "/api/v1/releases/"+releaseID+"/override",
		map[string]any{"reason": "Nothing to override, surely."}, bearer(h.login("admin@demo.agenttwin.dev"))); r.Status != 409 || errCode(r) != "NOTHING_TO_OVERRIDE" {
		t.Fatalf("override of a pass: %d %s", r.Status, r.Raw)
	}

	// No scenario for this change: nothing was simulated, which is a
	// warning, decided without asking the evaluation service.
	fx.set(func() { fx.scenarios = false })
	requests := h.count(`SELECT count(*) FROM control.outbox WHERE event_type = 'evaluation.run_requested.v1'`)
	empty := h.request("POST", "/api/v1/releases/"+releaseID+"/evaluate", nil, bearer(fx.eng))
	if empty.Status != http.StatusCreated || empty.Body["status"] != "DECIDED" || empty.Body["effective_outcome"] != "WARN" ||
		empty.Body["exit_code"] != 2.0 || empty.Body["ci_fails"] != false || empty.Body["eval_run_id"] != nil {
		t.Fatalf("empty suite: %d %s", empty.Status, empty.Raw)
	}
	if !slices.Equal(ruleNames(t, empty.Body), []string{"insufficient_coverage"}) {
		t.Errorf("rules = %v", ruleNames(t, empty.Body))
	}
	if n := h.count(`SELECT count(*) FROM control.outbox WHERE event_type = 'evaluation.run_requested.v1'`); n != requests {
		t.Error("an empty suite asked the evaluation service to run")
	}

	// With a policy that fails CI on a warning, CI fails.
	owner := h.login("owner@demo.agenttwin.dev")
	if r := h.request("PATCH", "/api/v1/projects/"+fx.pid, map[string]any{"gate_policy": map[string]any{"warnFailsCI": true}},
		bearer(owner)); r.Status != 200 {
		t.Fatalf("policy: %d %s", r.Status, r.Raw)
	}
	strict := h.request("POST", "/api/v1/releases/"+releaseID+"/evaluate", nil, bearer(fx.eng))
	if strict.Body["effective_outcome"] != "WARN" || strict.Body["exit_code"] != 2.0 || strict.Body["ci_fails"] != true ||
		strict.Body["policy"].(map[string]any)["warn_fails_ci"] != true {
		t.Fatalf("strict policy: %s", strict.Raw)
	}

	// Without the scenario library, the graph's scenarios cannot be pinned
	// and cannot run: their results are missing, so the gate blocks at once.
	fx.set(func() { fx.scenarios, fx.libraryDown = true, true })
	unpinned := h.request("POST", "/api/v1/releases/"+releaseID+"/evaluate", nil, bearer(fx.eng))
	if unpinned.Status != http.StatusCreated || unpinned.Body["effective_outcome"] != "BLOCK" ||
		unpinned.Body["decision"].(map[string]any)["incomplete"] != true {
		t.Fatalf("unpinned suite: %d %s", unpinned.Status, unpinned.Raw)
	}
	if suite := unpinned.Body["suite"].([]any); len(suite) != 2 {
		t.Fatalf("suite = %v, want the graph's two scenarios", suite)
	}
	for _, e := range unpinned.Body["suite"].([]any) {
		if _, pinned := e.(map[string]any)["scenario_version_id"]; pinned {
			t.Errorf("unconfirmed scenario pinned: %v", e)
		}
	}
	if !strings.Contains(string(unpinned.Raw), "could be pinned") || unpinned.Body["impact"].(map[string]any)["complete"] != false {
		t.Errorf("unpinned suite: %s", unpinned.Raw)
	}
	if s := unpinned.Body["summary"].(map[string]any); s["scenarios"] != 2.0 || s["evaluated"] != 0.0 {
		t.Errorf("unpinned summary = %v", s)
	}
	detail := h.request("GET", "/api/v1/releases/"+releaseID, nil, bearer(fx.eng))
	if n := len(detail.Body["revisions"].([]any)); n != 4 {
		t.Fatalf("revisions = %d, want 4", n)
	}
	if g := detail.Body["release"].(map[string]any)["gate"].(map[string]any); g["revision"] != 4.0 {
		t.Fatalf("the release shows revision %v, want the latest", g["revision"])
	}
}

// Who may create, read and gate releases, and what a request must say.
func TestReleaseAccessAndValidation(t *testing.T) {
	fx := newReleaseFixture(t)
	h := fx.h
	viewer := h.login("viewer@demo.agenttwin.dev")
	other := h.login("owner@other.agenttwin.dev")
	admin := h.login("admin@demo.agenttwin.dev")

	// A CI key creates and reads releases; it cannot override.
	key := h.request("POST", "/api/v1/projects/"+fx.pid+"/api-keys", map[string]any{"name": "ci", "scopes": []string{"ci"}}, bearer(admin))
	if key.Status != http.StatusCreated {
		t.Fatalf("key: %d %s", key.Status, key.Raw)
	}
	ci := map[string]string{"X-AgentTwin-Api-Key": key.Body["key"].(string), "Idempotency-Key": "ci-run-42-release"}
	body := map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.2.4",
		"candidate_version": "1.3.0", "evaluate": false}
	first := h.request("POST", "/api/v1/releases", body, ci)
	if first.Status != http.StatusCreated || first.Body["gate"] != nil || first.Body["release"].(map[string]any)["gate"] != nil {
		t.Fatalf("ci create: %d %s", first.Status, first.Raw)
	}
	releaseID := first.Body["release"].(map[string]any)["id"].(string)
	// A retried CI job with the same key does not create a second release.
	if again := h.request("POST", "/api/v1/releases", body, ci); again.Body["release"].(map[string]any)["id"] != releaseID {
		t.Fatalf("retried create: %s", again.Raw)
	}
	if n := h.count(`SELECT count(*) FROM control.release`); n != 1 {
		t.Fatalf("releases = %d", n)
	}
	if r := fx.gate(fx.eng, releaseID, 0); r.Status != 404 || errCode(r) != "NOT_EVALUATED" {
		t.Fatalf("gate before evaluating: %d %s", r.Status, r.Raw)
	}
	delete(ci, "Idempotency-Key")
	if r := h.request("POST", "/api/v1/releases/"+releaseID+"/evaluate", nil, ci); r.Status != http.StatusAccepted {
		t.Fatalf("ci evaluate: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/releases/"+releaseID+"/gate", nil, ci); r.Status != 200 {
		t.Fatalf("ci gate: %d %s", r.Status, r.Raw)
	}
	if r := h.request("POST", "/api/v1/releases/"+releaseID+"/override", map[string]any{"reason": "CI says it is fine."}, ci); r.Status != 403 {
		t.Fatalf("ci override: %d %s", r.Status, r.Raw)
	}
	if r := fx.gate(fx.eng, releaseID, 7); r.Status != 404 || errCode(r) != "NOT_FOUND" {
		t.Fatalf("unknown revision: %d %s", r.Status, r.Raw)
	}

	for _, c := range []struct {
		name, method, path, tok string
		body                    any
		status                  int
		code                    string
	}{
		{"viewer cannot create", "POST", "/api/v1/releases", viewer, map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.2.4", "candidate_version": "1.3.0"}, 403, "FORBIDDEN"},
		{"viewer cannot evaluate", "POST", "/api/v1/releases/" + releaseID + "/evaluate", viewer, nil, 403, "FORBIDDEN"},
		{"viewer reads the gate", "GET", "/api/v1/releases/" + releaseID + "/gate", viewer, nil, 200, ""},
		{"other org cannot see it", "GET", "/api/v1/releases/" + releaseID, other, nil, 404, "NOT_FOUND"},
		{"other org cannot read the gate", "GET", "/api/v1/releases/" + releaseID + "/gate", other, nil, 404, "NOT_FOUND"},
		{"other org cannot evaluate", "POST", "/api/v1/releases/" + releaseID + "/evaluate", other, nil, 404, "NOT_FOUND"},
		{"other org cannot list", "GET", "/api/v1/releases?project_id=" + fx.pid, other, nil, 404, "NOT_FOUND"},
		{"other org cannot create", "POST", "/api/v1/releases", other, map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.2.4", "candidate_version": "1.3.0"}, 404, "NOT_FOUND"},
		{"malformed id", "GET", "/api/v1/releases/nope", fx.eng, nil, 400, "INVALID_PARAMETER"},
		{"unknown release", "GET", "/api/v1/releases/" + ids.New(), fx.eng, nil, 404, "NOT_FOUND"},
		{"list needs a project", "GET", "/api/v1/releases", fx.eng, nil, 400, "INVALID_PARAMETER"},
		{"bad revision", "GET", "/api/v1/releases/" + releaseID + "/gate?revision=0", fx.eng, nil, 400, "INVALID_PARAMETER"},
		{"no project", "POST", "/api/v1/releases", fx.eng, map[string]any{"agent": "support-refund-agent", "baseline_version": "1.2.4", "candidate_version": "1.3.0"}, 400, "INVALID_RELEASE"},
		{"same versions", "POST", "/api/v1/releases", fx.eng, map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.3.0", "candidate_version": "1.3.0"}, 400, "INVALID_RELEASE"},
		{"unknown version", "POST", "/api/v1/releases", fx.eng, map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.2.4", "candidate_version": "9.9.9"}, 404, "NOT_FOUND"},
		{"no baseline", "POST", "/api/v1/releases", fx.eng, map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "", "candidate_version": "1.3.0"}, 400, "INVALID_RELEASE"},
		{"bad ci url", "POST", "/api/v1/releases", fx.eng, map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.2.4", "candidate_version": "1.3.0", "ci_url": "ftp://ci"}, 400, "INVALID_RELEASE"},
		{"unknown field", "POST", "/api/v1/releases", fx.eng, map[string]any{"project_id": fx.pid, "agent": "support-refund-agent", "baseline_version": "1.2.4", "candidate_version": "1.3.0", "outcome": "PASS"}, 400, "INVALID_JSON"},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := h.request(c.method, c.path, c.body, bearer(c.tok))
			if r.Status != c.status || (c.code != "" && errCode(r) != c.code) {
				t.Fatalf("%d %s, want %d %s", r.Status, r.Raw, c.status, c.code)
			}
		})
	}
	if r := h.request("POST", "/api/v1/releases", map[string]any{"project_id": fx.pid, "agent": "support-refund-agent",
		"baseline_version": "1.3.0", "candidate_version": "1.3.0"}, bearer(fx.eng)); r.Body["error"].(map[string]any)["details"].(map[string]any)["field"] != "candidate_version" {
		t.Errorf("same versions: %s", r.Raw)
	}
	if r := h.request("POST", "/api/v1/releases", map[string]any{"project_id": fx.pid, "agent": "support-refund-agent",
		"baseline_version": "", "candidate_version": "1.3.0"}, bearer(fx.eng)); r.Body["error"].(map[string]any)["details"].(map[string]any)["field"] != "baseline_version" {
		t.Errorf("no baseline: %s", r.Raw)
	}

	// Pages, newest first, and the agent filter.
	for range 2 {
		if r := fx.create(fx.eng, map[string]any{"evaluate": false}); r.Status != http.StatusCreated {
			t.Fatalf("create: %d %s", r.Status, r.Raw)
		}
	}
	page := h.request("GET", "/api/v1/releases?project_id="+fx.pid+"&limit=2", nil, bearer(viewer))
	if page.Status != 200 || len(page.Body["items"].([]any)) != 2 || page.Body["next_cursor"] == nil {
		t.Fatalf("page 1: %d %s", page.Status, page.Raw)
	}
	next := h.request("GET", "/api/v1/releases?project_id="+fx.pid+"&limit=2&cursor="+page.Body["next_cursor"].(string), nil, bearer(viewer))
	items := next.Body["items"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["id"] != releaseID || next.Body["next_cursor"] != nil {
		t.Fatalf("page 2: %s", next.Raw)
	}
	if r := h.request("GET", "/api/v1/releases?project_id="+fx.pid+"&agent=someone-else", nil, bearer(viewer)); len(r.Body["items"].([]any)) != 0 {
		t.Fatalf("unknown agent: %s", r.Raw)
	}
	if r := h.request("GET", "/api/v1/releases?project_id="+fx.pid+"&agent=support-refund-agent", nil, bearer(viewer)); len(r.Body["items"].([]any)) != 3 {
		t.Fatalf("agent filter: %s", r.Raw)
	}
}

// The gate counts a judge's verdicts only when the judge that graded the
// run is calibrated for the criterion (spec §28): the calibration the
// evaluation service reports is part of the decision's input.
func TestTheGateReadsTheJudgesCalibration(t *testing.T) {
	fx := newReleaseFixture(t)
	calibrated := deepCopy(fx.evidence).(map[string]any)
	judge := calibrated["run"].(map[string]any)["run"].(map[string]any)["judge"].(map[string]any)
	now := time.Now().UTC().Format(time.RFC3339)
	calibration := func(j map[string]any) map[string]any {
		return map[string]any{"id": ids.New(), "organization_id": fx.org, "project_id": fx.pid, "criterion": "correctness",
			"status": "COMPLETED", "example_count": 20, "examples_sha256": strings.Repeat("ab", 32),
			"judge": map[string]any{"provider": j["provider"], "model": j["model"], "kind": j["kind"],
				"prompt_version": j["prompt_version"], "prompt_sha256": j["prompt_sha256"]},
			"metrics": map[string]any{"examples": 20, "agreed": 19, "accuracy": 0.95, "kappa": 0.9, "errors": 0,
				"confusion": map[string]any{"true_pass": 10, "true_fail": 9, "false_pass": 1, "false_fail": 0}},
			"calibrated": true, "reason": nil, "error": nil, "requested_by": "user:reviewer", "created_at": now,
			"started_at": now, "finished_at": now}
	}
	other := deepCopy(judge).(map[string]any)
	other["prompt_sha256"] = strings.Repeat("0", 64)
	for _, c := range calibrated["judges"].(map[string]any)["criteria"].([]any) {
		c := c.(map[string]any)
		switch c["criterion"] {
		case "correctness":
			c["calibrated"], c["calibration"] = true, calibration(judge)
		case "relevance":
			// Calibrated, but for another prompt of the judge: not this run's judge.
			c["calibrated"], c["calibration"] = true, calibration(other)
		}
	}
	fx.set(func() { fx.evidence = calibrated })
	created := fx.create(fx.eng, nil)
	if _, err := fx.complete(t, gateEvaluationID(t, created), calibrated); err != nil {
		t.Fatal(err)
	}
	var agreement map[string]float64
	if err := fx.h.pool.QueryRow(context.Background(), `SELECT input->'judge_agreement' FROM control.gate_decision
		WHERE release_id = $1`, created.Body["release"].(map[string]any)["id"]).Scan(&agreement); err != nil {
		t.Fatal(err)
	}
	if len(agreement) != 1 || agreement["correctness"] != 0.95 {
		t.Fatalf("judge agreement = %v, want correctness only", agreement)
	}
}
