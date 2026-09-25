package server_test

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/openapicheck"
)

// fakeService stands in for the graph or the simulation service: it
// verifies the internal token, records who asked for what, answers from
// answer, and holds both the request and its answer to that service's
// own contract, so the control plane cannot drift from it unnoticed.
type fakeService struct {
	t        *testing.T
	service  string
	srv      *httptest.Server
	mu       sync.Mutex
	callers  []authn.Principal
	requests []map[string]any
}

func newFakeService(t *testing.T, service, document string, answer func(body map[string]any) (int, any)) *fakeService {
	t.Helper()
	contract, err := openapicheck.Load(contracts.OpenAPI, document)
	if err != nil {
		t.Fatal(err)
	}
	tokens, _ := authn.NewTokenService(internalSecret)
	f := &fakeService{t: t, service: service}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, _, err := tokens.Verify(authn.BearerToken(r), service)
		if err != nil {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		var body map[string]any
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		f.mu.Lock()
		f.callers = append(f.callers, p)
		f.requests = append(f.requests, body)
		f.mu.Unlock()
		status, out := answer(body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(out)
	})
	f.srv = httptest.NewServer(contract.Checking(handler, func(err error) {
		t.Errorf("%s contract: %v", service, err)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeService) calls() ([]authn.Principal, []map[string]any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.callers), slices.Clone(f.requests)
}

func step(kind, key string, edge any) map[string]any {
	s := map[string]any{"component": map[string]any{"kind": kind, "key": key}, "label": key, "edge": edge,
		"direction": nil, "confidence": nil, "sources": []string{}}
	if edge != nil {
		s["direction"], s["confidence"], s["sources"] = "down", 1.0, []string{"MANIFEST"}
	}
	return s
}

// blastRadius is what the graph service answers for the demo's prompt
// change: the changed prompt reaches the version's tools; two scenarios
// test them, a third is not in the library any more.
func blastRadius(promptKey string) map[string]any {
	prompt := step("PROMPT", promptKey, nil)
	version := step("AGENT_VERSION", "support-refund-agent@1.3.0", "USES")
	refund := step("TOOL", "refund_payment", "USES")
	linked := func(name, severity string, via map[string]any, direct bool, path ...map[string]any) map[string]any {
		c := via["component"].(map[string]any)
		return map[string]any{"component": map[string]any{"kind": "SCENARIO", "key": name}, "label": name,
			"severity": severity, "weight": 0.8, "reasons": []any{map[string]any{
				"via": c, "via_label": via["label"], "direct": direct, "score": 0.8,
				"path": append(path, step("SCENARIO", name, "TESTED_BY")),
			}}}
	}
	affected := func(s map[string]any, depth int, score float64) map[string]any {
		return map[string]any{"component": s["component"], "label": s["label"], "attributes": map[string]any{"risk": "WRITE_IRREVERSIBLE"},
			"seed": depth == 0, "direct": true, "depth": depth, "score": score, "severity": "critical", "certain": true,
			"factors": []string{"irreversible"}, "path": []any{prompt}}
	}
	refundAffected := affected(refund, 2, 0.9)
	return map[string]any{"blast_radius": map[string]any{
		"seeds":      []any{map[string]any{"component": map[string]any{"kind": "PROMPT", "key": promptKey}, "change": "modified"}},
		"unresolved": []any{},
		"affected":   []any{affected(prompt, 0, 1), refundAffected},
		"scenarios": []any{
			linked("refund-happy-path", "high", refund, true, prompt, version, refund),
			linked("gone-scenario", "low", refund, true, prompt, version, refund),
		},
		"policies": []any{}, "evaluators": []any{},
		"irreversible_actions": []any{refundAffected},
		"max_depth":            4, "truncated": false,
	}}
}

func matchedScenario(name, severity, source string, tags []string, nameMatch bool, matchedTags []string, sourceMatch bool, similar ...map[string]any) map[string]any {
	if similar == nil {
		similar = []map[string]any{}
	}
	suffix := strings.Repeat("0", 12-len(name)%10) + strings.Repeat("1", len(name)%10)
	return map[string]any{
		"id":   "01a0d7b0-77fb-709b-b8ff-" + suffix,
		"name": name, "agent": "support-refund-agent", "twin": "demo-co-support", "severity": severity, "tags": tags,
		"source": source, "latest_version": 1, "latest_version_id": "01a0d7b0-77fb-709b-b8fe-" + suffix,
		"description": name + " description",
		"matched":     map[string]any{"name": nameMatch, "tags": matchedTags, "source": sourceMatch, "similar": similar},
	}
}

// scenarioMatch is what the simulation service answers: the named
// scenario that still exists, the always-run suite, a known regression and
// a scenario close to the first query.
func scenarioMatch(body map[string]any) map[string]any {
	firstQuery := ""
	if qs, _ := body["queries"].([]any); len(qs) > 0 {
		firstQuery = qs[0].(map[string]any)["id"].(string)
	}
	return map[string]any{
		"project_id": body["project_id"], "agent": body["agent"], "embedding_model": "hashing-v1", "min_similarity": 0.25,
		"scenarios": []any{
			matchedScenario("refund-over-limit", "critical", "manual", []string{"refunds"}, false, []string{}, false,
				map[string]any{"query": firstQuery, "similarity": 0.4178}),
			matchedScenario("unauthorized-admin-tool", "critical", "manual", []string{"security"}, false, []string{"security"}, false),
			matchedScenario("refund-happy-path", "high", "manual", []string{"refunds", "smoke"}, true, []string{}, false,
				map[string]any{"query": firstQuery, "similarity": 0.3511}),
			matchedScenario("refund-charged-twice", "medium", "production_regression", []string{}, false, []string{}, true),
		},
		"unknown_names": []string{"gone-scenario"}, "unembedded_queries": []string{}, "truncated": false,
	}
}

type impactFixture struct {
	h                 *harness
	graph, simulation *fakeService
	pid, eng, csID    string
	promptKey         string
	// What the fakes answer besides their fixed answer.
	mu                             sync.Mutex
	graphTruncated, matchTruncated bool
}

func newImpactFixture(t *testing.T, graphUp, simulationUp bool) *impactFixture {
	t.Helper()
	fx := &impactFixture{}
	targets := map[string]string{}
	if graphUp {
		fx.graph = newFakeService(t, "graph-service", "openapi/graph-service.openapi.yaml", func(map[string]any) (int, any) {
			fx.mu.Lock()
			defer fx.mu.Unlock()
			out := blastRadius(fx.promptKey)
			out["blast_radius"].(map[string]any)["truncated"] = fx.graphTruncated
			return 200, out
		})
		targets["graph-service"] = fx.graph.srv.URL
	} else {
		// Configured, but nothing listens there.
		dead := httptest.NewServer(http.NotFoundHandler())
		targets["graph-service"] = dead.URL
		dead.Close()
	}
	if simulationUp {
		fx.simulation = newFakeService(t, "simulation-service", "openapi/simulation-service.openapi.yaml",
			func(body map[string]any) (int, any) {
				fx.mu.Lock()
				defer fx.mu.Unlock()
				out := scenarioMatch(body)
				out["truncated"] = fx.matchTruncated
				return 200, out
			})
		targets["simulation-service"] = fx.simulation.srv.URL
	}
	fx.h = newHarness(t, targets)
	fx.pid = fx.h.s.Demo.ProjectID
	fx.eng = fx.h.login("engineer@demo.agenttwin.dev")
	fx.h.registerDemo(fx.eng, fx.pid, "1.2.4", "1.3.0")
	r := fx.h.createChangeSet(fx.eng, fx.pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0"})
	if r.Status != http.StatusCreated {
		t.Fatalf("change set: %d %s", r.Status, r.Raw)
	}
	fx.csID = r.Body["id"].(string)
	fx.promptKey = r.Body["seeds"].([]any)[0].(map[string]any)["component"].(map[string]any)["key"].(string)
	return fx
}

func (fx *impactFixture) impact(tok string) resp {
	return fx.h.request("GET", "/api/v1/change-sets/"+fx.csID+"/impact", nil, bearer(tok))
}

func scenarioNames(r resp) []string {
	var out []string
	for _, s := range r.Body["scenarios"].([]any) {
		out = append(out, s.(map[string]any)["name"].(string))
	}
	return out
}

func scenarioOf(t *testing.T, r resp, name string) map[string]any {
	t.Helper()
	for _, s := range r.Body["scenarios"].([]any) {
		if m := s.(map[string]any); m["name"] == name {
			return m
		}
	}
	t.Fatalf("no scenario %s in %s", name, r.Raw)
	return nil
}

func TestAChangeSetsImpactSelectsScenariosAndSaysWhy(t *testing.T) {
	fx := newImpactFixture(t, true, true)
	r := fx.impact(fx.eng)
	if r.Status != 200 || r.Body["complete"] != true {
		t.Fatalf("impact: %d %s", r.Status, r.Raw)
	}
	// Severest first; the graph's scenario that is gone is reported, not listed.
	if got, want := scenarioNames(r), []string{"refund-over-limit", "unauthorized-admin-tool", "refund-happy-path", "refund-charged-twice"}; !slices.Equal(got, want) {
		t.Fatalf("scenarios = %v, want %v", got, want)
	}
	if got := strs(r.Body["unlinked_scenarios"]); !slices.Equal(got, []string{"gone-scenario"}) {
		t.Errorf("unlinked = %v", got)
	}
	happy := scenarioOf(t, r, "refund-happy-path")
	// The version the library selected is what a release pins.
	if happy["latest_version_id"] != "01a0d7b0-77fb-709b-b8fe-"+strings.Repeat("0", 12-len("refund-happy-path")%10)+
		strings.Repeat("1", len("refund-happy-path")%10) {
		t.Errorf("latest_version_id = %v", happy["latest_version_id"])
	}
	why := strs(happy["why"])
	if len(why) != 2 || why[0] != "tests tool refund_payment, which the change to the prompt reaches in 2 steps" ||
		!strings.HasPrefix(why[1], "its description is close to the change of the prompt (similarity 0.35)") {
		t.Errorf("why = %q", why)
	}
	g := happy["reasons"].(map[string]any)["graph"].([]any)[0].(map[string]any)
	if g["hops"] != 3.0 || g["from"].(map[string]any)["kind"] != "PROMPT" || g["via"].(map[string]any)["key"] != "refund_payment" {
		t.Errorf("graph reason = %v", g)
	}
	sim := happy["reasons"].(map[string]any)["similar"].([]any)[0].(map[string]any)
	if item := sim["item"].(map[string]any); item["kind"] != "prompt" || item["subject"] != fx.promptKey || item["change"] != "modified" {
		t.Errorf("similar item = %v", item)
	}
	if w := strs(scenarioOf(t, r, "unauthorized-admin-tool")["why"]); !slices.Equal(w, []string{"always runs (tagged security)"}) {
		t.Errorf("always-run why = %v", w)
	}
	if w := strs(scenarioOf(t, r, "refund-charged-twice")["why"]); !slices.Equal(w, []string{"a known production regression"}) {
		t.Errorf("regression why = %v", w)
	}
	counts := r.Body["counts"].(map[string]any)
	if counts["scenarios"] != 4.0 || counts["graph"] != 1.0 || counts["similar"] != 2.0 || counts["always_run"] != 1.0 || counts["known_regression"] != 1.0 {
		t.Errorf("counts = %v", counts)
	}
	if acts := r.Body["irreversible_actions"].([]any); len(acts) != 1 || acts[0].(map[string]any)["label"] != "refund_payment" {
		t.Errorf("irreversible actions = %v", acts)
	}
	pol := r.Body["policy"].(map[string]any)
	if !slices.Equal(strs(pol["always_run_tags"]), []string{"critical", "security"}) || pol["include_known_regressions"] != true || pol["max_depth"] != 4.0 {
		t.Errorf("policy = %v", pol)
	}

	// What the services were asked, and as whom: the engineer, in this project only.
	callers, graphReqs := fx.graph.calls()
	simCallers, simReqs := fx.simulation.calls()
	for _, p := range append(callers, simCallers...) {
		if p.Actor != principalActor(t, fx.h, fx.eng) || p.AllProjects || !slices.Equal(p.ProjectIDs, []string{fx.pid}) {
			t.Errorf("a service was asked as %+v", p)
		}
	}
	gr := graphReqs[0]
	if gr["project_id"] != fx.pid || gr["max_depth"] != 4.0 ||
		gr["scope"].(map[string]any)["agent"] != "support-refund-agent" || gr["scope"].(map[string]any)["version"] != "1.3.0" {
		t.Errorf("graph request = %v", gr)
	}
	if seeds := gr["changes"].([]any); len(seeds) != 1 || seeds[0].(map[string]any)["component"].(map[string]any)["key"] != fx.promptKey {
		t.Errorf("seeds = %v", seeds)
	}
	sr := simReqs[0]
	if !slices.Equal(strs(sr["names"]), []string{"refund-happy-path", "gone-scenario"}) ||
		!slices.Equal(strs(sr["tags"]), []string{"critical", "security"}) || !slices.Equal(strs(sr["sources"]), []string{"production_regression"}) ||
		sr["agent"] != "support-refund-agent" || sr["project_id"] != fx.pid {
		t.Errorf("simulation request = %v", sr)
	}
	// The query is what changed in the prompt, as stored (secrets masked).
	qs := sr["queries"].([]any)
	if len(qs) != 1 || !strings.Contains(qs[0].(map[string]any)["text"].(string), "Issue eligible refunds immediately") {
		t.Errorf("queries = %v", qs)
	}
}

// principalActor is the actor of a session, as the control plane names it.
func principalActor(t *testing.T, h *harness, tok string) string {
	t.Helper()
	me := h.request("GET", "/api/v1/me", nil, bearer(tok))
	if me.Status != 200 {
		t.Fatalf("me: %d %s", me.Status, me.Raw)
	}
	return me.Body["principal"].(map[string]any)["sub"].(string)
}

// A viewer may read the impact; prompt text is used to find similar
// scenarios but never shown to them, and the answer names no query text.
func TestAViewerReadsTheImpactWithoutThePromptText(t *testing.T) {
	fx := newImpactFixture(t, true, true)
	viewer := fx.h.login("viewer@demo.agenttwin.dev")
	r := fx.impact(viewer)
	if r.Status != 200 || len(scenarioNames(r)) != 4 {
		t.Fatalf("viewer: %d %s", r.Status, r.Raw)
	}
	if strings.Contains(string(r.Raw), "Issue eligible refunds") || strings.Contains(string(r.Raw), "Customer satisfaction") {
		t.Fatalf("the impact shows prompt text: %s", r.Raw)
	}
}

func TestPolicyDecidesWhatIsAlwaysRunAndHowFar(t *testing.T) {
	fx := newImpactFixture(t, true, true)
	owner := fx.h.login("owner@demo.agenttwin.dev")
	policy := map[string]any{"alwaysRunTags": []string{"smoke"}, "includeKnownRegressions": false, "maxDepth": 2}
	if r := fx.h.request("PATCH", "/api/v1/projects/"+fx.pid, map[string]any{"gate_policy": policy}, bearer(owner)); r.Status != 200 {
		t.Fatalf("policy: %d %s", r.Status, r.Raw)
	}
	r := fx.impact(fx.eng)
	if r.Status != 200 {
		t.Fatalf("impact: %d %s", r.Status, r.Raw)
	}
	_, graphReqs := fx.graph.calls()
	_, simReqs := fx.simulation.calls()
	if graphReqs[0]["max_depth"] != 2.0 || !slices.Equal(strs(simReqs[0]["tags"]), []string{"smoke"}) || len(simReqs[0]["sources"].([]any)) != 0 {
		t.Errorf("asked with %v and %v", graphReqs[0], simReqs[0])
	}
	if pol := r.Body["policy"].(map[string]any); pol["max_depth"] != 2.0 || pol["include_known_regressions"] != false {
		t.Errorf("policy = %v", pol)
	}
}

func TestAnImpactWithoutAServiceIsIncompleteNotAnError(t *testing.T) {
	// The graph service does not answer: the library's selection stands, marked incomplete.
	fx := newImpactFixture(t, false, true)
	r := fx.impact(fx.eng)
	if r.Status != 200 || r.Body["complete"] != false || r.Body["graph"] != nil {
		t.Fatalf("without the graph: %d %s", r.Status, r.Raw)
	}
	probs := r.Body["problems"].([]any)
	if len(probs) != 1 || probs[0].(map[string]any)["service"] != "graph-service" || probs[0].(map[string]any)["code"] != "UPSTREAM_UNAVAILABLE" {
		t.Errorf("problems = %v", probs)
	}
	_, simReqs := fx.simulation.calls()
	if len(simReqs[0]["names"].([]any)) != 0 {
		t.Errorf("without the graph, no scenario is asked for by name: %v", simReqs[0])
	}
	if !slices.Contains(strs(r.Body["notes"]), "Without the dependency graph, scenarios linked to the changed components are missing.") {
		t.Errorf("notes = %v", r.Body["notes"])
	}

	// The simulation service is not configured: the graph's links are listed, unconfirmed.
	fx = newImpactFixture(t, true, false)
	r = fx.impact(fx.eng)
	if r.Status != 200 || r.Body["complete"] != false || r.Body["embedding_model"] != "" {
		t.Fatalf("without the library: %d %s", r.Status, r.Raw)
	}
	if got := scenarioNames(r); !slices.Equal(got, []string{"refund-happy-path", "gone-scenario"}) {
		t.Errorf("scenarios = %v", got)
	}
	if s := scenarioOf(t, r, "gone-scenario"); s["in_library"] != false || s["latest_version_id"] != nil {
		t.Errorf("unconfirmed scenario = %v", s)
	}
	probs = r.Body["problems"].([]any)
	if len(probs) != 1 || probs[0].(map[string]any)["code"] != "NOT_CONFIGURED" {
		t.Errorf("problems = %v", probs)
	}
}

func TestTheImpactStaysInItsProject(t *testing.T) {
	fx := newImpactFixture(t, true, true)
	other := fx.h.login("owner@other.agenttwin.dev")
	if r := fx.impact(other); r.Status != 404 {
		t.Errorf("another organization: %d %s", r.Status, r.Raw)
	}
	if r := fx.h.request("GET", "/api/v1/change-sets/not-a-uuid/impact", nil, bearer(fx.eng)); r.Status != 400 || errCode(r) != "INVALID_PARAMETER" {
		t.Errorf("bad id: %d %s", r.Status, r.Raw)
	}
	if r := fx.h.request("GET", "/api/v1/change-sets/01a0d7b0-77fb-709b-b8ff-000000000000/impact", nil, bearer(fx.eng)); r.Status != 404 {
		t.Errorf("unknown id: %d", r.Status)
	}
	// A key of another project of the organization may not.
	owner := fx.h.login("owner@demo.agenttwin.dev")
	proj := fx.h.request("POST", "/api/v1/projects", map[string]any{"slug": "billing", "name": "Billing"}, bearer(owner))
	key := fx.h.request("POST", "/api/v1/projects/"+proj.Body["id"].(string)+"/api-keys",
		map[string]any{"name": "reader", "scopes": []string{"read"}}, bearer(owner))
	if key.Status != http.StatusCreated {
		t.Fatalf("key: %d %s", key.Status, key.Raw)
	}
	if r := fx.h.request("GET", "/api/v1/change-sets/"+fx.csID+"/impact", nil, map[string]string{"X-AgentTwin-Api-Key": key.Body["key"].(string)}); r.Status != 404 {
		t.Errorf("another project's key: %d %s", r.Status, r.Raw)
	}
	// A key that may not read (it only writes traces) may not.
	writer := fx.h.request("POST", "/api/v1/projects/"+fx.pid+"/api-keys",
		map[string]any{"name": "ingest", "scopes": []string{"traces:write"}}, bearer(owner))
	if writer.Status != http.StatusCreated {
		t.Fatalf("key: %d %s", writer.Status, writer.Raw)
	}
	if r := fx.h.request("GET", "/api/v1/change-sets/"+fx.csID+"/impact", nil, map[string]string{"X-AgentTwin-Api-Key": writer.Body["key"].(string)}); r.Status != 403 {
		t.Errorf("a key without read: %d %s", r.Status, r.Raw)
	}
	// A CI key of the project may read it.
	if r := fx.h.request("GET", "/api/v1/change-sets/"+fx.csID+"/impact", nil, map[string]string{"X-AgentTwin-Api-Key": demoKey}); r.Status != 200 {
		t.Errorf("CI key: %d %s", r.Status, r.Raw)
	}
	// Nobody else was asked about this project.
	callers, _ := fx.graph.calls()
	for _, p := range callers {
		if p.OrgID != fx.h.s.Demo.OrganizationID {
			t.Errorf("a service was asked for another organization: %+v", p)
		}
	}
}

func TestAnImpactCutShortSaysSo(t *testing.T) {
	fx := newImpactFixture(t, true, true)
	set := func(graph, match bool) resp {
		fx.mu.Lock()
		fx.graphTruncated, fx.matchTruncated = graph, match
		fx.mu.Unlock()
		r := fx.impact(fx.eng)
		if r.Status != 200 || len(r.Body["problems"].([]any)) != 0 {
			t.Fatalf("impact: %d %s", r.Status, r.Raw)
		}
		return r
	}
	r := set(true, false)
	if r.Body["complete"] != false || r.Body["graph"].(map[string]any)["truncated"] != true ||
		!slices.Contains(strs(r.Body["notes"]), "The blast radius stopped at its node bound; components further away were not reached.") {
		t.Errorf("graph cut short: %s", r.Raw)
	}
	r = set(false, true)
	if r.Body["complete"] != false || r.Body["truncated"] != true ||
		!slices.Contains(strs(r.Body["notes"]), "More scenarios were selected than one answer lists; the named ones come first.") {
		t.Errorf("library cut short: %s", r.Raw)
	}
	if r = set(false, false); r.Body["complete"] != true || len(r.Body["notes"].([]any)) != 0 {
		t.Errorf("in full: %s", r.Raw)
	}
}

// Two versions that differ only in their number change nothing the graph
// knows: it is not asked; the library still answers the always-run suite
// and the known regressions.
func TestAChangeSetWithNothingToTraverseAsksOnlyTheLibrary(t *testing.T) {
	fx := newImpactFixture(t, true, true)
	renumbered := strings.Replace(string(readManifest(t, "1.3.0")), "version: 1.3.0", "version: 1.3.9", 1)
	if r := fx.h.registerManifest(fx.eng, fx.pid, []byte(renumbered)); r.Status != http.StatusCreated {
		t.Fatalf("register: %d %s", r.Status, r.Raw)
	}
	cs := fx.h.createChangeSet(fx.eng, fx.pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.3.0", "candidate_version": "1.3.9"})
	if cs.Status != http.StatusCreated || len(cs.Body["seeds"].([]any)) != 0 {
		t.Fatalf("change set: %d %s", cs.Status, cs.Raw)
	}
	r := fx.h.request("GET", "/api/v1/change-sets/"+cs.Body["id"].(string)+"/impact", nil, bearer(fx.eng))
	if r.Status != 200 || r.Body["complete"] != true {
		t.Fatalf("impact: %d %s", r.Status, r.Raw)
	}
	if calls, _ := fx.graph.calls(); len(calls) != 0 {
		t.Errorf("the graph was asked %d times", len(calls))
	}
	_, simReqs := fx.simulation.calls()
	if len(simReqs) != 1 || len(simReqs[0]["names"].([]any)) != 0 || len(simReqs[0]["queries"].([]any)) != 0 {
		t.Errorf("simulation request = %v", simReqs)
	}
	// Named or similar, without a change to link them to: not part of it.
	if got := scenarioNames(r); !slices.Equal(got, []string{"unauthorized-admin-tool", "refund-charged-twice"}) {
		t.Errorf("scenarios = %v", got)
	}
	if g := r.Body["graph"].(map[string]any); len(g["seeds"].([]any)) != 0 || g["affected_count"] != 0.0 {
		t.Errorf("graph = %v", g)
	}
}
