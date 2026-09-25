package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/xml"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/openapicheck"
)

// The fixtures in testdata are the control plane's real answers, captured
// by its integration tests (AGENTTWIN_CAPTURE_DIR): the demo's release
// 1.2.4 → 1.3.0, blocked, overridden, a candidate that passes, and an empty
// suite that warns.

var contract = func() *openapicheck.Contract {
	c, err := openapicheck.Load(contracts.OpenAPI, "openapi/control-plane.openapi.yaml")
	if err != nil {
		panic(err)
	}
	return c
}()

func fixture(t *testing.T, name string) json.RawMessage {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func field(t *testing.T, raw json.RawMessage, key string) json.RawMessage {
	t.Helper()
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	return m[key]
}

type recorded struct {
	method, path string
	header       http.Header
	body         map[string]any
}

// fakeControlPlane answers the CLI from the fixtures, holding every
// exchange to the control plane's contract.
type fakeControlPlane struct {
	t   *testing.T
	srv *httptest.Server
	mu  sync.Mutex
	// gates are answered in order to GET .../gate; the last one repeats.
	gates []json.RawMessage
	// fail answers a path (method + path prefix) with these statuses first.
	fail     map[string][]int
	requests []recorded
	// notEvaluated: GET .../gate answers NOT_EVALUATED until an evaluation is requested.
	notEvaluated bool
}

func newFake(t *testing.T, gates ...string) *fakeControlPlane {
	t.Helper()
	f := &fakeControlPlane{t: t, fail: map[string][]int{}}
	for _, g := range gates {
		f.gates = append(f.gates, fixture(t, g))
	}
	created := fixture(t, "release-created")
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		f.mu.Lock()
		defer f.mu.Unlock()
		f.requests = append(f.requests, recorded{method: r.Method, path: r.URL.Path, header: r.Header.Clone(), body: body})
		for prefix, statuses := range f.fail {
			if len(statuses) > 0 && strings.HasPrefix(r.Method+" "+r.URL.Path, prefix) {
				f.fail[prefix] = statuses[1:]
				writeError(w, statuses[0])
				return
			}
		}
		if r.Header.Get("X-AgentTwin-Api-Key") != "atk_test" {
			writeError(w, http.StatusUnauthorized)
			return
		}
		write := func(status int, v json.RawMessage) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_, _ = w.Write(v)
		}
		p := r.URL.Path
		switch {
		case r.Method == "GET" && p == "/health/ready":
			write(200, json.RawMessage(`{"status":"ok"}`))
		case r.Method == "GET" && p == "/api/v1/me":
			write(200, fixture(t, "me-ci"))
		case r.Method == "GET" && p == "/api/v1/projects":
			write(200, fixture(t, "projects-ci"))
		case r.Method == "POST" && p == "/api/v1/manifests/validate":
			write(200, fixture(t, "manifest-validation"))
		case r.Method == "POST" && strings.HasSuffix(p, "/agent-manifests"):
			write(200, fixture(t, "registered-version"))
		case r.Method == "POST" && p == "/api/v1/releases":
			write(201, created)
		case r.Method == "GET" && strings.HasSuffix(p, "/gate"):
			if f.notEvaluated {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(404)
				_, _ = w.Write([]byte(`{"error":{"code":"NOT_EVALUATED","message":"The release has not been evaluated yet.","request_id":"r1"}}`))
				return
			}
			g := f.gates[0]
			if len(f.gates) > 1 {
				f.gates = f.gates[1:]
			}
			write(200, g)
		case r.Method == "POST" && strings.HasSuffix(p, "/evaluate"):
			f.notEvaluated = false
			write(202, field(t, created, "gate"))
		case r.Method == "GET" && strings.HasPrefix(p, "/api/v1/releases/"):
			write(200, json.RawMessage(`{"release":`+string(field(t, created, "release"))+`,"revisions":[]}`))
		default:
			t.Errorf("unexpected %s %s", r.Method, p)
			writeError(w, 404)
		}
	})
	f.srv = httptest.NewServer(contract.Checking(h, func(err error) { t.Errorf("contract: %v", err) }))
	t.Cleanup(f.srv.Close)
	return f
}

func writeError(w http.ResponseWriter, status int) {
	codes := map[int]string{401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMITED",
		500: "INTERNAL", 502: "UPSTREAM_UNAVAILABLE", 503: "UNAVAILABLE"}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]any{"code": codes[status], "message": "fake " + codes[status],
		"request_id": "req-fake"}})
}

func (f *fakeControlPlane) calls(method, pathSuffix string) []recorded {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []recorded
	for _, r := range f.requests {
		if r.method == method && strings.HasSuffix(r.path, pathSuffix) {
			out = append(out, r)
		}
	}
	return out
}

type result struct {
	code           int
	stdout, stderr string
}

// run runs the CLI with an environment of vars against f.
func run(t *testing.T, f *fakeControlPlane, vars map[string]string, args ...string) result {
	t.Helper()
	var stdout, stderr bytes.Buffer
	env := map[string]string{"AGENTTWIN_API_KEY": "atk_test"}
	if f != nil {
		env["AGENTTWIN_URL"] = f.srv.URL
	}
	for k, v := range vars {
		env[k] = v
	}
	code := Main(context.Background(), args, Env{Stdout: &stdout, Stderr: &stderr, Stdin: strings.NewReader(env["STDIN"]),
		Getenv: func(k string) string { return env[k] }, Now: time.Now, Sleep: sleep})
	return result{code: code, stdout: stdout.String(), stderr: stderr.String()}
}

// demoProjectID is the project of the captured responses.
var demoProjectID = func() string {
	raw, err := os.ReadFile(filepath.Join("testdata", "projects-ci.json"))
	if err != nil {
		panic(err)
	}
	var page struct {
		Items []struct {
			ID string `json:"id"`
		} `json:"items"`
	}
	if err := json.Unmarshal(raw, &page); err != nil || len(page.Items) == 0 {
		panic("testdata/projects-ci.json names no project")
	}
	return page.Items[0].ID
}()

var checkArgs = []string{"release", "check", "--project", demoProjectID, "--baseline", "support-refund-agent@1.2.4",
	"--candidate", "support-refund-agent@1.3.0", "--poll-interval", "1ms"}

func TestABadCandidateFailsCIWithTheEvidence(t *testing.T) {
	f := newFake(t, "gate-pending", "gate-pending", "gate-block")
	github := map[string]string{"GITHUB_SHA": "4E5F6A7B8C9D", "GITHUB_SERVER_URL": "https://github.com",
		"GITHUB_REPOSITORY": "acme/agents", "GITHUB_RUN_ID": "42", "AGENTTWIN_WEB_URL": "https://agenttwin.example.com/"}
	r := run(t, f, github, append(checkArgs, "--ci", "--title", "Refund flow rewrite")...)
	if r.code != ExitBlock {
		t.Fatalf("exit %d, want 3\nstdout:\n%s\nstderr:\n%s", r.code, r.stdout, r.stderr)
	}
	for _, want := range []string{
		"Gate         BLOCK",
		"Required scenarios passed                    6/9   missing: refund-happy-path",
		"BLOCK  An action took effect twice (duplicate_side_effect)",
		"• refund-timeout-after-mutation (no-double-refund): The side effect refund:ORD-1001 was applied 2 times.",
		"first divergence: At step 2 the baseline called get_refund_policy",
		"trace: 4ad026574fb37d447a7a79f1eecf00ac",
		"             Blocked: an action took effect twice (1); success the final state disproves (1); a new\n",
		"WARN   Costlier than the baseline (cost_regression)",
		"(verified)",
		"Details      https://agenttwin.example.com/releases/",
		"Exit code: 3",
	} {
		if !strings.Contains(r.stdout, want) {
			t.Errorf("report lacks %q:\n%s", want, r.stdout)
		}
	}
	// A scenario's divergence and trace are said once per rule.
	if n := strings.Count(r.stdout, "trace: 4ad026574fb37d447a7a79f1eecf00ac"); n != 3 {
		t.Errorf("the timeout scenario's trace appears %d times, want once in each of its 3 rules", n)
	}
	// The release names the commit and the CI run the environment gave.
	creates := f.calls("POST", "/api/v1/releases")
	if len(creates) != 1 {
		t.Fatalf("creates = %d", len(creates))
	}
	body := creates[0].body
	if body["project_id"] != demoProjectID || body["agent"] != "support-refund-agent" || body["baseline_version"] != "1.2.4" ||
		body["candidate_version"] != "1.3.0" || body["title"] != "Refund flow rewrite" ||
		body["ci_url"] != "https://github.com/acme/agents/actions/runs/42" ||
		body["git"].(map[string]any)["candidate_commit"] != "4e5f6a7b8c9d" {
		t.Errorf("create body = %v", body)
	}
	key := creates[0].header.Get("Idempotency-Key")
	if !strings.HasPrefix(key, "agenttwin-release-") {
		t.Errorf("Idempotency-Key = %q", key)
	}
	if gets := f.calls("GET", "/gate"); len(gets) != 3 {
		t.Errorf("gate reads = %d, want 3 (pending, pending, decided)", len(gets))
	}

	// A retried CI job sends the same key: the control plane answers the
	// release it created the first time.
	f2 := newFake(t, "gate-block")
	run(t, f2, github, append(checkArgs, "--ci", "--title", "Refund flow rewrite")...)
	if again := f2.calls("POST", "/api/v1/releases")[0].header.Get("Idempotency-Key"); again != key {
		t.Errorf("retry key %q, first %q", again, key)
	}
}

func TestExitCodesFollowTheGate(t *testing.T) {
	for _, c := range []struct {
		gate     string
		ci       bool
		want     int
		contains string
	}{
		{"gate-pass", false, ExitPass, "Gate         PASS"},
		{"gate-pass", true, ExitPass, "Exit code: 0"},
		{"gate-warn", false, ExitWarn, "Gate         WARN"},
		// The policy does not fail CI on a warning.
		{"gate-warn", true, ExitPass, "Exit code: 0"},
		// This one does.
		{"gate-warn-fails-ci", true, ExitWarn, "Exit code: 2"},
		{"gate-block", true, ExitBlock, "Exit code: 3"},
		{"gate-overridden", true, ExitPass, "Gate         OVERRIDDEN (originally BLOCK)"},
	} {
		t.Run(c.gate, func(t *testing.T) {
			args := checkArgs
			if c.ci {
				args = append(args, "--ci")
			}
			r := run(t, newFake(t, c.gate), nil, args...)
			if r.code != c.want || !strings.Contains(r.stdout, c.contains) {
				t.Fatalf("exit %d, want %d; report:\n%s\n%s", r.code, c.want, r.stdout, r.stderr)
			}
		})
	}
	r := run(t, newFake(t, "gate-overridden"), nil, checkArgs...)
	for _, want := range []string{"Override     by user:", "Hotfix for the outage", "Ticket       https://tickets.example.com/OPS-12"} {
		if !strings.Contains(r.stdout, want) {
			t.Errorf("overridden report lacks %q:\n%s", want, r.stdout)
		}
	}
}

func TestJUnitReport(t *testing.T) {
	dir := t.TempDir()
	out := filepath.Join(dir, "gate.xml")
	r := run(t, newFake(t, "gate-block"), nil, append(checkArgs, "--ci", "--format", "junit", "--output", out)...)
	if r.code != ExitBlock || !strings.Contains(r.stdout, "Gate         BLOCK") {
		t.Fatalf("exit %d; stdout:\n%s", r.code, r.stdout)
	}
	raw, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	var doc junitSuites
	if err := xml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("junit: %v\n%s", err, raw)
	}
	cases := map[string]junitCase{}
	for _, c := range doc.Suites[0].Cases {
		cases[c.Classname+"/"+c.Name] = c
	}
	for name, fails := range map[string]bool{
		"agenttwin.release/gate":                           true,
		"agenttwin.scenario/refund-timeout-after-mutation": true,
		"agenttwin.scenario/refund-tool-success-lie":       true,
		"agenttwin.scenario/refund-over-limit":             false,
		"agenttwin.scenario/refund-happy-path":             false, // a warning; this policy does not fail CI on one
		"agenttwin.rule/cost_regression":                   false,
		"agenttwin.rule/semantic_regression":               false,
		"agenttwin.scenario/unauthorized-admin-tool":       false,
		"agenttwin.scenario/cross-tenant-order":            false,
		"agenttwin.scenario/malicious-retrieved-content":   false,
		"agenttwin.scenario/refund-prompt-injection":       false,
		"agenttwin.scenario/refund-rate-limited":           false,
	} {
		c, ok := cases[name]
		if !ok {
			t.Errorf("no test case %s", name)
			continue
		}
		if (c.Failure != nil) != fails {
			t.Errorf("%s failed = %v, want %v", name, c.Failure != nil, fails)
		}
	}
	if doc.Failures != 3 || doc.Tests != len(doc.Suites[0].Cases) {
		t.Errorf("tests %d failures %d", doc.Tests, doc.Failures)
	}
	if c := cases["agenttwin.scenario/refund-timeout-after-mutation"]; !strings.Contains(c.Failure.Text, "trace: 4ad026574fb37d447a7a79f1eecf00ac") {
		t.Errorf("failure text = %q", c.Failure.Text)
	}
	if c := cases["agenttwin.scenario/refund-happy-path"]; !strings.Contains(c.SystemOut, "[WARN]") {
		t.Errorf("warning not reported: %+v", c)
	}

	// Outside CI a warning fails its test; an override fails nothing and says so.
	raw2, _ := renderJUnit(reportInput{release: Release{ID: "r"}, gate: decode(t, "gate-block"), exit: ExitBlock}, false)
	if !bytes.Contains(raw2, []byte(`name="refund-happy-path" classname="agenttwin.scenario">`+"\n"+`      <failure`)) {
		t.Errorf("warning outside CI did not fail:\n%s", raw2)
	}
	raw3, _ := renderJUnit(reportInput{release: Release{ID: "r"}, gate: decode(t, "gate-overridden"), exit: ExitPass}, true)
	if bytes.Contains(raw3, []byte("<failure")) || !bytes.Contains(raw3, []byte("Originally BLOCK; overridden by")) {
		t.Errorf("overridden junit:\n%s", raw3)
	}
}

func decode(t *testing.T, name string) Gate {
	t.Helper()
	var g Gate
	if err := json.Unmarshal(fixture(t, name), &g); err != nil {
		t.Fatal(err)
	}
	return g
}

func TestJSONReport(t *testing.T) {
	r := run(t, newFake(t, "gate-block"), nil, append(checkArgs, "--format", "json")...)
	var out struct {
		Release  Release `json:"release"`
		ExitCode int     `json:"exit_code"`
		Gate     struct {
			Decision struct {
				Outcome string `json:"outcome"`
				Rules   []Rule `json:"rules"`
			} `json:"decision"`
			Suite []any `json:"suite"`
		} `json:"gate"`
	}
	if err := json.Unmarshal([]byte(r.stdout), &out); err != nil {
		t.Fatalf("json: %v\n%s", err, r.stdout)
	}
	if r.code != ExitBlock || out.ExitCode != ExitBlock || out.Gate.Decision.Outcome != "BLOCK" || len(out.Gate.Decision.Rules) != 7 ||
		len(out.Gate.Suite) != 9 || out.Release.Agent.Name != "support-refund-agent" {
		t.Fatalf("json report = %s", r.stdout)
	}
}

func TestInfrastructureAndConfigurationErrorsExit4(t *testing.T) {
	f := newFake(t, "gate-pending")
	cases := []struct {
		name  string
		vars  map[string]string
		args  []string
		error string
	}{
		{"no credential", map[string]string{"AGENTTWIN_API_KEY": ""}, checkArgs, "no credential"},
		{"no project", nil, []string{"release", "check", "--baseline", "a@1", "--candidate", "a@2"}, "the project is required"},
		{"no agent", nil, []string{"release", "check", "--project", demoProjectID, "--baseline", "1", "--candidate", "2"}, "the agent is required"},
		{"two agents", nil, []string{"release", "check", "--project", demoProjectID, "--baseline", "a@1", "--candidate", "b@2"}, "different agents"},
		{"same version", nil, []string{"release", "check", "--project", demoProjectID, "--agent", "a", "--baseline", "1", "--candidate", "1"}, "the same version"},
		{"bad commit", nil, append(checkArgs, "--commit", "not-a-sha"), "--commit must be a git commit"},
		{"bad format", nil, append(checkArgs, "--format", "xml"), "--format must be text, json or junit"},
		{"bad ci url", nil, append(checkArgs, "--ci-url", "ftp://x"), "--ci-url must be an http(s) URL"},
		{"bad url", map[string]string{"AGENTTWIN_URL": "localhost:8080"}, checkArgs, "--url must be an http(s) URL"},
		{"unknown command", nil, []string{"deploy"}, `unknown command "deploy"`},
		{"unknown subcommand", nil, []string{"release", "ship"}, `unknown subcommand "ship"`},
		{"extra argument", nil, append(checkArgs, "now"), `unexpected argument "now"`},
		{"unknown project", nil, []string{"release", "check", "--project", "nope", "--baseline", "a@1", "--candidate", "a@2"}, `no project "nope"`},
		{"bad release id", nil, []string{"release", "check", "--release", "x"}, "--release must be a release id"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := run(t, f, c.vars, c.args...)
			if r.code != ExitError || !strings.Contains(r.stderr, c.error) {
				t.Fatalf("exit %d, stderr %q, want 4 and %q", r.code, r.stderr, c.error)
			}
		})
	}

	t.Run("unreachable", func(t *testing.T) {
		dead := httptest.NewServer(http.NotFoundHandler())
		dead.Close()
		r := run(t, nil, map[string]string{"AGENTTWIN_URL": dead.URL}, checkArgs...)
		if r.code != ExitError || !strings.Contains(r.stderr, "cannot reach AgentTwin") || strings.Count(r.stderr, "retrying") != maxAttempts-1 {
			t.Fatalf("exit %d: %s", r.code, r.stderr)
		}
	})
	t.Run("revoked key", func(t *testing.T) {
		r := run(t, f, map[string]string{"AGENTTWIN_API_KEY": "atk_revoked"}, checkArgs...)
		if r.code != ExitError || !strings.Contains(r.stderr, "401 UNAUTHENTICATED") || !strings.Contains(r.stderr, "Check AGENTTWIN_API_KEY") {
			t.Fatalf("exit %d: %s", r.code, r.stderr)
		}
	})
	t.Run("forbidden", func(t *testing.T) {
		g := newFake(t, "gate-block")
		g.fail["POST /api/v1/releases"] = []int{403}
		r := run(t, g, nil, checkArgs...)
		if r.code != ExitError || !strings.Contains(r.stderr, `a CI key (scope "ci")`) {
			t.Fatalf("exit %d: %s", r.code, r.stderr)
		}
	})
	t.Run("the gate does not decide in time", func(t *testing.T) {
		r := run(t, newFake(t, "gate-pending"), nil, append(checkArgs, "--timeout", "100ms")...)
		if r.code != ExitError || !strings.Contains(r.stderr, "did not decide within 100ms") ||
			!strings.Contains(r.stderr, "agenttwin release check --release ") {
			t.Fatalf("exit %d: %s", r.code, r.stderr)
		}
	})
	t.Run("help", func(t *testing.T) {
		if r := run(t, f, nil, "--help"); r.code != ExitPass || !strings.Contains(r.stdout, "Exit codes: 0 pass, 2 warn, 3 block, 4") {
			t.Fatalf("help: %d %s", r.code, r.stdout)
		}
		if r := run(t, f, nil, "release", "check", "-h"); r.code != ExitPass || !strings.Contains(r.stderr, "-baseline") {
			t.Fatalf("release check -h: %d %s", r.code, r.stderr)
		}
		if r := run(t, f, nil, "release", "check", "--bogus"); r.code != ExitError || strings.Count(r.stderr, "bogus") != 1 {
			t.Fatalf("bad flag: %d %s", r.code, r.stderr)
		}
	})
}

func TestTransientFailuresAreRetried(t *testing.T) {
	f := newFake(t, "gate-pending", "gate-block")
	f.fail["POST /api/v1/releases"] = []int{429, 429}
	f.fail["GET /api/v1/releases/"] = []int{429}
	r := run(t, f, nil, checkArgs...)
	if r.code != ExitBlock {
		t.Fatalf("exit %d: %s", r.code, r.stderr)
	}
	creates := f.calls("POST", "/api/v1/releases")
	if len(creates) != 3 || creates[0].header.Get("Idempotency-Key") != creates[2].header.Get("Idempotency-Key") {
		t.Fatalf("creates = %d; a retry must reuse the key", len(creates))
	}
	if strings.Count(r.stderr, "retrying") != 3 {
		t.Errorf("stderr = %s", r.stderr)
	}
	// Gateways answer these too.
	for _, status := range []int{429, 502, 503, 504} {
		if !Transient(&APIError{Status: status}) {
			t.Errorf("%d is not transient", status)
		}
	}
	if Transient(&APIError{Status: 409, Code: "ALREADY_OVERRIDDEN"}) || !Transient(&APIError{Status: 409, Code: "IDEMPOTENCY_IN_PROGRESS"}) {
		t.Error("409s")
	}
	// A failure that persists gives up after a few attempts, not at the timeout.
	p := newFake(t, "gate-block")
	p.fail["POST /api/v1/releases"] = []int{429, 429, 429, 429, 429, 429, 429}
	if r := run(t, p, nil, checkArgs...); r.code != ExitError || len(p.calls("POST", "/api/v1/releases")) != maxAttempts {
		t.Fatalf("persistent 429: %d after %d attempts", r.code, len(p.calls("POST", "/api/v1/releases")))
	}
	if backoff(time.Second, 1) != time.Second || backoff(time.Second, 3) != 4*time.Second || backoff(time.Second, 20) != maxBackoff {
		t.Error("backoff")
	}
	// A refusal is not retried.
	g := newFake(t, "gate-block")
	g.fail["POST /api/v1/releases"] = []int{500}
	if r := run(t, g, nil, checkArgs...); r.code != ExitError || len(g.calls("POST", "/api/v1/releases")) != 1 {
		t.Fatalf("500 retried: %d %s", r.code, r.stderr)
	}
}

func TestExistingReleasesAndProjectSlugs(t *testing.T) {
	var created struct {
		Release Release `json:"release"`
	}
	f := newFake(t, "gate-block")
	_ = json.Unmarshal(fixture(t, "release-created"), &created)

	// An existing release that was evaluated: its gate, nothing created.
	r := run(t, f, nil, "release", "check", "--release", created.Release.ID, "--poll-interval", "1ms")
	if r.code != ExitBlock || len(f.calls("POST", "/api/v1/releases")) != 0 || len(f.calls("POST", "/evaluate")) != 0 {
		t.Fatalf("existing release: %d %s", r.code, r.stderr)
	}
	// One that was not: it is evaluated first.
	g := newFake(t, "gate-pending", "gate-block")
	g.notEvaluated = true
	if r := run(t, g, nil, "release", "check", "--release", created.Release.ID, "--poll-interval", "1ms"); r.code != ExitBlock ||
		len(g.calls("POST", "/evaluate")) != 1 {
		t.Fatalf("unevaluated release: %d %s", r.code, r.stderr)
	}
	// --reevaluate asks for a new revision.
	h := newFake(t, "gate-block")
	if r := run(t, h, nil, append(checkArgs, "--reevaluate")...); r.code != ExitBlock || len(h.calls("POST", "/evaluate")) != 1 {
		t.Fatalf("reevaluate: %d %s", r.code, r.stderr)
	}
	// --no-wait returns once the evaluation is requested.
	n := newFake(t, "gate-pending")
	if r := run(t, n, nil, append(checkArgs, "--no-wait")...); r.code != ExitPass || len(n.calls("GET", "/gate")) != 0 {
		t.Fatalf("no-wait: %d %s", r.code, r.stderr)
	}
	// A project named by its slug.
	s := newFake(t, "gate-block")
	args := []string{"release", "check", "--project", "customer-support", "--agent", "support-refund-agent",
		"--baseline", "1.2.4", "--candidate", "1.3.0", "--poll-interval", "1ms"}
	var projects struct {
		Items []struct {
			Slug string `json:"slug"`
		} `json:"items"`
	}
	_ = json.Unmarshal(fixture(t, "projects-ci"), &projects)
	args[3] = projects.Items[0].Slug
	if r := run(t, s, nil, args...); r.code != ExitBlock || s.calls("POST", "/api/v1/releases")[0].body["project_id"] != demoProjectID {
		t.Fatalf("slug: %d %s", r.code, r.stderr)
	}
}

func TestChangedFiles(t *testing.T) {
	f := newFake(t, "gate-block")
	list := "agent/prompt.md\n\nagent/tools.py\nagent/prompt.md\n"
	run(t, f, map[string]string{"STDIN": list}, append(checkArgs, "--changed-files", "-", "--base-commit", "0A1B2C3D")...)
	git := f.calls("POST", "/api/v1/releases")[0].body["git"].(map[string]any)
	files := git["changed_files"].([]any)
	if len(files) != 2 || files[0] != "agent/prompt.md" || files[1] != "agent/tools.py" || git["base_commit"] != "0a1b2c3d" {
		t.Fatalf("git = %v", git)
	}
	if r := run(t, f, nil, append(checkArgs, "--changed-files", filepath.Join(t.TempDir(), "missing"))...); r.code != ExitError {
		t.Fatalf("missing list: %d", r.code)
	}
}

func TestDoctorAndAgentCommands(t *testing.T) {
	f := newFake(t, "gate-block")
	r := run(t, f, map[string]string{"AGENTTWIN_PROJECT": demoProjectID}, "doctor")
	if r.code != ExitPass || !strings.Contains(r.stdout, "✓ credential") || !strings.Contains(r.stdout, "can create and check releases") ||
		!strings.Contains(r.stdout, "✓ project") {
		t.Fatalf("doctor: %d\n%s%s", r.code, r.stdout, r.stderr)
	}
	down := newFake(t, "gate-block")
	down.fail["GET /health/ready"] = []int{503}
	if r := run(t, down, nil, "doctor"); r.code != ExitError || !strings.Contains(r.stdout, "✗ control plane") {
		t.Fatalf("doctor down: %d %s", r.code, r.stdout)
	}

	manifest := filepath.Join("..", "..", "demo", "support-refund-agent", "manifests", "1.3.0.yaml")
	if r := run(t, f, nil, "agent", "validate", manifest); r.code != ExitPass || !strings.Contains(r.stdout, "support-refund-agent@1.3.0 is valid") {
		t.Fatalf("validate: %d %s %s", r.code, r.stdout, r.stderr)
	}
	r = run(t, f, map[string]string{"AGENTTWIN_PROJECT": demoProjectID}, "agent", "register", "--commit", "4E5F6A7B", manifest)
	if r.code != ExitPass || !strings.Contains(r.stdout, "already registered with this content") {
		t.Fatalf("register: %d %s %s", r.code, r.stdout, r.stderr)
	}
	reg := f.calls("POST", "/agent-manifests")
	if len(reg) != 1 || reg[0].header.Get("Content-Type") != "application/yaml" {
		t.Fatalf("register request = %+v", reg)
	}
	if r := run(t, f, nil, "agent", "register", manifest); r.code != ExitError || !strings.Contains(r.stderr, "the project is required") {
		t.Fatalf("register without project: %d %s", r.code, r.stderr)
	}
	if r := run(t, f, nil, "agent", "validate", "missing.yaml"); r.code != ExitError {
		t.Fatalf("validate missing file: %d", r.code)
	}
}

func TestTextReportDetails(t *testing.T) {
	g := decode(t, "gate-block")
	// Ratios with nothing to cover are not shown.
	if out := renderText(reportInput{gate: g, exit: ExitBlock}); strings.Contains(out, "Known regressions replayed") {
		t.Errorf("a 0/0 ratio is shown:\n%s", out)
	}
	// Long evidence is cut, and says where the rest is.
	var many []Evidence
	for i := range 7 {
		many = append(many, Evidence{Summary: "case " + string(rune('a'+i))})
	}
	g.Decision.Rules = []Rule{{Rule: "critical_failure", Outcome: "BLOCK", Title: "T", Statement: "S", Evidence: many}}
	out := renderText(reportInput{gate: g, exit: ExitBlock})
	if !strings.Contains(out, "• case e") || strings.Contains(out, "• case f") || !strings.Contains(out, "… and 2 more (see --format json)") {
		t.Errorf("evidence not cut at %d:\n%s", maxEvidence, out)
	}
	// Evidence that no longer matches its hash is said loudly.
	no := false
	g.EvidenceVerified = &no
	if out := renderText(reportInput{gate: g, exit: ExitBlock}); !strings.Contains(out, "NOT VERIFIED") || strings.Contains(out, "(verified)") {
		t.Errorf("unverified evidence:\n%s", out)
	}
}

func TestExitForAGateThatHasNotDecided(t *testing.T) {
	three := 3
	for _, g := range []Gate{
		{Status: "EVALUATING"},
		{Status: "DECIDED"},                        // no decision
		{Status: "DECIDED", Decision: &Decision{}}, // no exit code
		{Status: "EVALUATING", Decision: &Decision{}, ExitCode: &three},
	} {
		if g.Decided() || ExitFor(g, true) != ExitError {
			t.Errorf("%+v counts as decided", g)
		}
	}
	if g := (Gate{Status: "DECIDED", Decision: &Decision{}, ExitCode: &three}); !g.Decided() || ExitFor(g, true) != ExitBlock {
		t.Error("a decided gate")
	}
}
