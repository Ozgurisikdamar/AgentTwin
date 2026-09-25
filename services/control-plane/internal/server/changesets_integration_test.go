package server_test

import (
	"context"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
)

// registerDemo registers demo versions of support-refund-agent.
func (h *harness) registerDemo(tok, projectID string, versions ...string) {
	h.t.Helper()
	for _, v := range versions {
		if r := h.registerManifest(tok, projectID, readManifest(h.t, v)); r.Status != http.StatusCreated && r.Status != http.StatusOK {
			h.t.Fatalf("register %s: %d %s", v, r.Status, r.Raw)
		}
	}
}

func (h *harness) createChangeSet(tok, projectID string, body map[string]any) resp {
	h.t.Helper()
	return h.request("POST", "/api/v1/projects/"+projectID+"/change-sets", body, bearer(tok))
}

func itemsOf(t *testing.T, cs map[string]any) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, it := range cs["items"].([]any) {
		out = append(out, it.(map[string]any))
	}
	return out
}

func strs(v any) []string {
	var out []string
	for _, s := range v.([]any) {
		out = append(out, s.(string))
	}
	return out
}

func TestAChangeSetDescribesWhatChangedBetweenTwoVersions(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	h.registerDemo(eng, pid, "1.2.4", "1.3.0")

	body := map[string]any{
		"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0",
		"title": "Refund instructions rewrite",
		"git": map[string]any{"base_commit": "0a1b2c3d", "candidate_commit": "4e5f6a7b",
			"changed_files": []string{"agent/prompt.md", "agent/tools.py", "agent/prompt.md"}},
		"declared": []map[string]any{
			{"kind": "policy", "name": "refund-limit", "change": "modified", "summary": "limit 500 → 300"},
			{"kind": "dataset", "name": "refunds-golden", "change": "added"},
		},
	}
	r := h.createChangeSet(eng, pid, body)
	if r.Status != http.StatusCreated || r.Body["created"] != true {
		t.Fatalf("create: %d %s", r.Status, r.Raw)
	}
	cs := r.Body
	if cs["agent_name"] != "support-refund-agent" || cs["base"].(map[string]any)["version"] != "1.2.4" ||
		cs["candidate"].(map[string]any)["version"] != "1.3.0" || cs["title"] != "Refund instructions rewrite" {
		t.Fatalf("change set header: %s", r.Raw)
	}

	var kinds []string
	var prompt map[string]any
	for _, it := range itemsOf(t, cs) {
		kinds = append(kinds, it["kind"].(string)+":"+it["confidence"].(string))
		if it["kind"] == "prompt" {
			prompt = it
		}
	}
	if want := []string{"prompt:exact", "code:filenames_only", "policy:declared", "dataset:declared"}; !slices.Equal(kinds, want) {
		t.Fatalf("items = %v, want %v", kinds, want)
	}
	detail := prompt["detail"].(map[string]any)
	if len(detail["diff"].([]any)) == 0 {
		t.Fatalf("an engineer (settings.read) sees the prompt diff: %v", detail)
	}
	wantMentions := []string{"get_refund_policy", "lookup_order", "refund_payment"}
	if got := strs(detail["mentions"]); !slices.Equal(got, wantMentions) {
		t.Errorf("mentions = %v, want %v", got, wantMentions)
	}
	// The repeated file is recorded once.
	for _, it := range itemsOf(t, cs) {
		if it["kind"] == "code" {
			if files := strs(it["detail"].(map[string]any)["changed_files"]); !slices.Equal(files, []string{"agent/prompt.md", "agent/tools.py"}) {
				t.Errorf("changed files = %v", files)
			}
		}
	}

	var seeds []string
	for _, s := range cs["seeds"].([]any) {
		c := s.(map[string]any)["component"].(map[string]any)
		seeds = append(seeds, c["kind"].(string)+":"+c["key"].(string))
	}
	cand := h.request("GET", "/api/v1/agents/"+cs["agent_id"].(string)+"/versions/1.3.0", nil, bearer(eng)).Body
	want := []string{"PROMPT:" + cand["prompt_sha256"].(string), "AGENT_VERSION:support-refund-agent@1.3.0",
		"DATASET:refunds-golden", "POLICY:refund-limit"}
	if !slices.Equal(seeds, want) {
		t.Errorf("seeds = %v, want %v", seeds, want)
	}
	if sc := cs["scope"].(map[string]any); sc["agent"] != "support-refund-agent" || sc["version"] != "1.3.0" {
		t.Errorf("scope = %v", sc)
	}
	sum := cs["summary"].(map[string]any)
	if sum["items"] != 4.0 || sum["seeds"] != 4.0 || sum["breaking"] != 0.0 || sum["kinds"].(map[string]any)["prompt"] != 1.0 ||
		sum["confidence"].(map[string]any)["declared"] != 2.0 {
		t.Errorf("summary = %v", sum)
	}

	// The same request is the same change set: no copy, no second audit entry.
	again := h.createChangeSet(eng, pid, body)
	if again.Status != http.StatusOK || again.Body["created"] != false || again.Body["id"] != cs["id"] {
		t.Fatalf("repeat: %d %s", again.Status, again.Raw)
	}
	if n := h.count(`SELECT count(*) FROM control.change_set`); n != 1 {
		t.Fatalf("change sets stored = %d, want 1", n)
	}
	if n := h.count(`SELECT count(*) FROM control.audit_event WHERE action = 'change_set.created' AND resource_id = $1`, cs["id"]); n != 1 {
		t.Fatalf("audit entries = %d, want 1", n)
	}
	// Declared changes in another order, with stray spaces, are the same
	// request.
	reordered := map[string]any{}
	for k, v := range body {
		reordered[k] = v
	}
	reordered["declared"] = []map[string]any{
		{"kind": "dataset", "name": " refunds-golden ", "change": "added"},
		{"kind": "policy", "name": "refund-limit", "change": "modified", "summary": "limit 500 → 300"},
	}
	if r := h.createChangeSet(eng, pid, reordered); r.Status != http.StatusOK || r.Body["id"] != cs["id"] {
		t.Fatalf("same content: %d %s", r.Status, r.Raw)
	}

	got := h.request("GET", "/api/v1/change-sets/"+cs["id"].(string), nil, bearer(eng))
	if got.Status != 200 || len(itemsOf(t, got.Body)) != 4 || got.Body["content_sha256"] != cs["content_sha256"] {
		t.Fatalf("get: %d %s", got.Status, got.Raw)
	}
	if _, has := got.Body["created"]; has {
		t.Error("a read change set carries created")
	}

	// A change set is evidence: the database refuses to rewrite it.
	if _, err := h.pool.Exec(context.Background(), `UPDATE control.change_set SET title = 'rewritten'`); err == nil ||
		!strings.Contains(err.Error(), "append-only") {
		t.Fatalf("update of a change set: %v", err)
	}
}

// Prompt text is configuration: a caller without settings.read sees that a
// prompt changed and which tools it names, not its lines (as for versions).
func TestAPromptDiffIsShownOnlyToWhoMayReadPrompts(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	viewer := h.login("viewer@demo.agenttwin.dev")
	h.registerDemo(eng, pid, "1.2.4", "1.3.0")

	// A CI key may compare versions (release.write) but not read prompts.
	ci := map[string]string{"Authorization": "Bearer " + demoKey}
	r := h.request("POST", "/api/v1/projects/"+pid+"/change-sets",
		map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0"}, ci)
	if r.Status != http.StatusCreated {
		t.Fatalf("CI key: %d %s", r.Status, r.Raw)
	}
	id := r.Body["id"].(string)
	for who, resp := range map[string]resp{
		"ci key": r,
		"viewer": h.request("GET", "/api/v1/change-sets/"+id, nil, bearer(viewer)),
	} {
		d := itemsOf(t, resp.Body)[0]["detail"].(map[string]any)
		if d["diff"] != nil || d["diff_truncated"] != nil || !strings.HasPrefix(fmt.Sprint(d["diff_unavailable"]), "hidden") {
			t.Errorf("%s sees the diff: %v", who, d)
		}
		if len(strs(d["mentions"])) != 3 {
			t.Errorf("%s: mentions are tool names, not prompt text: %v", who, d)
		}
	}
	// It is hidden when read, not lost: the engineer sees it.
	d := itemsOf(t, h.request("GET", "/api/v1/change-sets/"+id, nil, bearer(eng)).Body)[0]["detail"].(map[string]any)
	if len(d["diff"].([]any)) == 0 {
		t.Errorf("stored diff lost: %v", d)
	}
	if r := h.createChangeSet(viewer, pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0", "title": "x"}); r.Status != 403 {
		t.Errorf("viewer creates: %d", r.Status)
	}
}

func TestWithoutStoredPromptTextAChangeIsKnownByHash(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	owner := h.login("owner@demo.agenttwin.dev")
	if r := h.request("PATCH", "/api/v1/projects/"+pid, map[string]any{"store_prompt_text": false}, bearer(owner)); r.Status != 200 {
		t.Fatalf("settings: %d %s", r.Status, r.Raw)
	}
	h.registerDemo(owner, pid, "1.2.4", "1.3.0")
	r := h.createChangeSet(owner, pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0"})
	if r.Status != http.StatusCreated {
		t.Fatalf("create: %d %s", r.Status, r.Raw)
	}
	it := itemsOf(t, r.Body)[0]
	d := it["detail"].(map[string]any)
	if it["confidence"] != "hash_only" || d["diff"] != nil || d["diff_unavailable"] != "the project does not store prompt text" {
		t.Fatalf("prompt item = %v", it)
	}
}

func TestChangeSetRequestsAreValidated(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	h.registerDemo(eng, pid, "1.2.4", "1.3.0")
	base := func(extra map[string]any) map[string]any {
		b := map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0"}
		for k, v := range extra {
			b[k] = v
		}
		return b
	}
	many := make([]string, 1001)
	for i := range many {
		many[i] = fmt.Sprintf("f%d.py", i)
	}
	for _, c := range []struct {
		name  string
		body  map[string]any
		want  int
		code  string
		field string
	}{
		{"same version", base(map[string]any{"candidate_version": "1.2.4"}), 400, "INVALID_CHANGE_SET", "candidate_version"},
		{"missing base", base(map[string]any{"base_version": ""}), 400, "INVALID_CHANGE_SET", "base_version"},
		{"bad agent name", base(map[string]any{"agent": "Not An Agent"}), 400, "INVALID_CHANGE_SET", "agent"},
		{"unknown agent", base(map[string]any{"agent": "nobody"}), 404, "NOT_FOUND", ""},
		{"unknown version", base(map[string]any{"candidate_version": "9.9.9"}), 404, "NOT_FOUND", ""},
		{"bad commit", base(map[string]any{"git": map[string]any{"base_commit": "HEAD"}}), 400, "INVALID_CHANGE_SET", "git.base_commit"},
		{"too many files", base(map[string]any{"git": map[string]any{"changed_files": many}}), 400, "INVALID_CHANGE_SET", "git.changed_files"},
		{"empty file name", base(map[string]any{"git": map[string]any{"changed_files": []string{" "}}}), 400, "INVALID_CHANGE_SET", "git.changed_files"},
		{"declared kind", base(map[string]any{"declared": []map[string]any{{"kind": "tool", "name": "x", "change": "added"}}}), 400, "INVALID_CHANGE_SET", "declared[0].kind"},
		{"declared change", base(map[string]any{"declared": []map[string]any{{"kind": "policy", "name": "x", "change": "renamed"}}}), 400, "INVALID_CHANGE_SET", "declared[0].change"},
		{"declared twice", base(map[string]any{"declared": []map[string]any{
			{"kind": "policy", "name": "x", "change": "added"}, {"kind": "policy", "name": "x", "change": "removed"}}}), 400, "INVALID_CHANGE_SET", "declared[1].name"},
		{"title control char", base(map[string]any{"title": "a\x07b"}), 400, "INVALID_CHANGE_SET", "title"},
		{"long title", base(map[string]any{"title": strings.Repeat("é", 201)}), 400, "INVALID_CHANGE_SET", "title"},
		{"unknown field", base(map[string]any{"candidate": "1.3.0"}), 400, "INVALID_JSON", ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := h.createChangeSet(eng, pid, c.body)
			if r.Status != c.want || errCode(r) != c.code {
				t.Fatalf("%d %s, want %d %s", r.Status, r.Raw, c.want, c.code)
			}
			if c.field != "" {
				if f := r.Body["error"].(map[string]any)["details"].(map[string]any)["field"]; f != c.field {
					t.Errorf("field = %v, want %s", f, c.field)
				}
			}
		})
	}
	if n := h.count(`SELECT count(*) FROM control.change_set`); n != 0 {
		t.Fatalf("a rejected request stored %d change sets", n)
	}
	for _, path := range []string{"/api/v1/change-sets/not-a-uuid", "/api/v1/projects/" + pid + "/change-sets?cursor=not~base64",
		"/api/v1/projects/" + pid + "/change-sets?limit=0", "/api/v1/projects/" + pid + "/change-sets?agent=Bad%20Name"} {
		if r := h.request("GET", path, nil, bearer(eng)); r.Status != 400 {
			t.Errorf("GET %s = %d %s", path, r.Status, r.Raw)
		}
	}
}

func TestChangeSetsAreListedNewestFirstAndPaged(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	h.registerDemo(eng, pid, "1.2.3", "1.2.4", "1.3.0")
	var ids []string
	for _, pair := range [][2]string{{"1.2.3", "1.2.4"}, {"1.2.4", "1.3.0"}, {"1.2.3", "1.3.0"}} {
		r := h.createChangeSet(eng, pid, map[string]any{"agent": "support-refund-agent", "base_version": pair[0], "candidate_version": pair[1]})
		if r.Status != http.StatusCreated {
			t.Fatalf("create %v: %d %s", pair, r.Status, r.Raw)
		}
		ids = append([]string{r.Body["id"].(string)}, ids...)
	}
	var seen []string
	cursor := ""
	for pages := 0; ; pages++ {
		if pages > 3 {
			t.Fatal("paging does not end")
		}
		path := "/api/v1/projects/" + pid + "/change-sets?limit=2"
		if cursor != "" {
			path += "&cursor=" + cursor
		}
		r := h.request("GET", path, nil, bearer(eng))
		if r.Status != 200 {
			t.Fatalf("list: %d %s", r.Status, r.Raw)
		}
		for _, it := range items(t, r) {
			if _, has := it["items"]; has {
				t.Fatal("a listed change set carries its items")
			}
			seen = append(seen, it["id"].(string))
		}
		next, _ := r.Body["next_cursor"].(string)
		if next == "" {
			break
		}
		cursor = next
	}
	if !slices.Equal(seen, ids) {
		t.Fatalf("listed %v, want newest first %v", seen, ids)
	}
	if r := h.request("GET", "/api/v1/projects/"+pid+"/change-sets?agent=support-refund-agent", nil, bearer(eng)); len(items(t, r)) != 3 {
		t.Errorf("agent filter: %s", r.Raw)
	}
	if r := h.request("GET", "/api/v1/projects/"+pid+"/change-sets?agent=nobody", nil, bearer(eng)); r.Status != 200 || len(items(t, r)) != 0 || r.Body["next_cursor"] != nil {
		t.Errorf("unknown agent: %d %s", r.Status, r.Raw)
	}
}

func TestChangeSetsStayInTheirOrganization(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	other := h.login("owner@other.agenttwin.dev")
	h.registerDemo(eng, pid, "1.2.4", "1.3.0")
	r := h.createChangeSet(eng, pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0"})
	if r.Status != http.StatusCreated {
		t.Fatalf("create: %d %s", r.Status, r.Raw)
	}
	id := r.Body["id"].(string)
	for _, c := range []struct{ method, path string }{
		{"GET", "/api/v1/change-sets/" + id},
		{"GET", "/api/v1/projects/" + pid + "/change-sets"},
		{"POST", "/api/v1/projects/" + pid + "/change-sets"},
	} {
		var body any
		if c.method == "POST" {
			body = map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0", "title": "probe"}
		}
		if r := h.request(c.method, c.path, body, bearer(other)); r.Status != 404 {
			t.Errorf("other org %s %s = %d %s", c.method, c.path, r.Status, r.Raw)
		}
	}
}

// An API key names one project: a key of another project of the same
// organization does not reach a change set.
func TestAProjectKeySeesOnlyItsProjectsChangeSets(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	owner := h.login("owner@demo.agenttwin.dev")
	h.registerDemo(owner, pid, "1.2.4", "1.3.0")
	r := h.createChangeSet(owner, pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0"})
	if r.Status != http.StatusCreated {
		t.Fatalf("create: %d %s", r.Status, r.Raw)
	}
	id := r.Body["id"].(string)
	proj := h.request("POST", "/api/v1/projects", map[string]any{"slug": "billing", "name": "Billing"}, bearer(owner))
	if proj.Status != http.StatusCreated {
		t.Fatalf("project: %d %s", proj.Status, proj.Raw)
	}
	key := h.request("POST", "/api/v1/projects/"+proj.Body["id"].(string)+"/api-keys",
		map[string]any{"name": "reader", "scopes": []string{"read"}}, bearer(owner))
	if key.Status != http.StatusCreated {
		t.Fatalf("key: %d %s", key.Status, key.Raw)
	}
	hdr := map[string]string{"X-AgentTwin-Api-Key": key.Body["key"].(string)}
	if r := h.request("GET", "/api/v1/change-sets/"+id, nil, hdr); r.Status != 404 {
		t.Errorf("another project's key reads the change set: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/projects/"+pid+"/change-sets", nil, hdr); r.Status != 404 {
		t.Errorf("another project's key lists change sets: %d %s", r.Status, r.Raw)
	}
	// The demo project's own key does.
	if r := h.request("GET", "/api/v1/change-sets/"+id, nil, map[string]string{"X-AgentTwin-Api-Key": demoKey}); r.Status != 200 {
		t.Errorf("the project's key: %d %s", r.Status, r.Raw)
	}
}

// The schema holds a change set's versions to its agent and its agent to its
// project, whatever a use case gets wrong.
func TestTheSchemaKeepsAChangeSetsVersionsInItsAgent(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	owner := h.login("owner@demo.agenttwin.dev")
	h.registerDemo(owner, pid, "1.2.4", "1.3.0")
	renamed := strings.Replace(string(readManifest(t, "1.2.4")), "name: support-refund-agent", "name: billing-agent", 1)
	if r := h.registerManifest(owner, pid, []byte(renamed)); r.Status != http.StatusCreated {
		t.Fatalf("second agent: %d %s", r.Status, r.Raw)
	}
	other := h.request("POST", "/api/v1/projects", map[string]any{"slug": "billing", "name": "Billing"}, bearer(owner))
	if other.Status != http.StatusCreated {
		t.Fatalf("project: %d %s", other.Status, other.Raw)
	}
	if r := h.createChangeSet(owner, pid, map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0"}); r.Status != http.StatusCreated {
		t.Fatalf("create: %d %s", r.Status, r.Raw)
	}
	ctx := context.Background()
	var foreignVersion string
	if err := h.pool.QueryRow(ctx, `SELECT v.id FROM control.agent_version v JOIN control.agent a ON a.id = v.agent_id
		WHERE a.name = 'billing-agent'`).Scan(&foreignVersion); err != nil {
		t.Fatal(err)
	}
	// A copy of the stored row with its project or base version replaced:
	// only the replacement can make the insert fail.
	for _, c := range []struct {
		name          string
		project, base any
		refused       bool
	}{
		{"a consistent copy", nil, nil, false},
		{"a base version of another agent", nil, foreignVersion, true},
		{"the agent in another project", other.Body["id"], nil, true},
	} {
		_, err := h.pool.Exec(ctx, `INSERT INTO control.change_set (id, organization_id, project_id, agent_id, base_version_id,
			candidate_version_id, items, seeds, scope, summary, content_sha256, created_by)
			SELECT gen_random_uuid(), organization_id, COALESCE($1::uuid, project_id), agent_id, COALESCE($2::uuid, base_version_id),
				candidate_version_id, items, seeds, scope, summary, md5($3) || md5($3), created_by
			FROM control.change_set ORDER BY created_at LIMIT 1`, c.project, c.base, c.name)
		if refused := err != nil; refused != c.refused {
			t.Errorf("%s: err = %v", c.name, err)
		}
	}
}

func TestConcurrentEqualChangeSetsAreStoredOnce(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	h.registerDemo(eng, pid, "1.2.4", "1.3.0")
	body := map[string]any{"agent": "support-refund-agent", "base_version": "1.2.4", "candidate_version": "1.3.0", "title": "race"}
	statuses := make([]int, 8)
	idsSeen := make([]string, 8)
	var wg sync.WaitGroup
	for i := range statuses {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			r := h.createChangeSet(eng, pid, body)
			statuses[i] = r.Status
			idsSeen[i], _ = r.Body["id"].(string)
		}(i)
	}
	wg.Wait()
	created := 0
	for i, s := range statuses {
		switch s {
		case http.StatusCreated:
			created++
		case http.StatusOK:
		default:
			t.Fatalf("status %d in %v", s, statuses)
		}
		if idsSeen[i] != idsSeen[0] {
			t.Fatalf("different ids: %v", idsSeen)
		}
	}
	if created != 1 || h.count(`SELECT count(*) FROM control.change_set`) != 1 {
		t.Fatalf("created %d (%v), stored %d", created, statuses, h.count(`SELECT count(*) FROM control.change_set`))
	}
}
