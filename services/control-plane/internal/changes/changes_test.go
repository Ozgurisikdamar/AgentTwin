package changes

import (
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
)

// demo parses a demo manifest and keeps its prompt text.
func demo(t *testing.T, version string) Version {
	t.Helper()
	raw, err := os.ReadFile("../../../../demo/support-refund-agent/manifests/" + version + ".yaml")
	if err != nil {
		t.Fatal(err)
	}
	m, err := domain.ParseManifest(raw)
	if err != nil {
		t.Fatal(err)
	}
	text := m.Instructions
	return Version{Manifest: *m, PromptText: &text}
}

func parse(t *testing.T, doc string) Version {
	t.Helper()
	m, err := domain.ParseManifest([]byte(doc))
	if err != nil {
		t.Fatalf("%v\n%s", err, doc)
	}
	text := m.Instructions
	return Version{Manifest: *m, PromptText: &text}
}

func kinds(s Set) []string {
	var out []string
	for _, it := range s.Items {
		out = append(out, it.Kind+":"+it.Subject+":"+it.Change)
	}
	return out
}

func seedsOf(s Set) []string {
	var out []string
	for _, sd := range s.Seeds {
		out = append(out, sd.Component.Kind+":"+sd.Component.Key+":"+sd.Change)
	}
	return out
}

func TestAPromptChangeIsDiffedAndNamesTheToolsItTouches(t *testing.T) {
	base, cand := demo(t, "1.2.4"), demo(t, "1.3.0")
	s := Compute(base, cand, nil, nil)
	if got := kinds(s); !slices.Equal(got, []string{"prompt:" + cand.Manifest.PromptSHA256 + ":modified"}) {
		t.Fatalf("1.2.4 → 1.3.0 changes only the prompt, got %v", got)
	}
	it := s.Items[0]
	if it.Confidence != ConfidenceExact || it.Detail["base_sha256"] != base.Manifest.PromptSHA256 {
		t.Errorf("prompt item = %+v", it)
	}
	// Removed lines name get_refund_policy and lookup_order; added ones,
	// refund_payment. A tool the diff does not touch is not named.
	want := []string{"get_refund_policy", "lookup_order", "refund_payment"}
	if got := it.Detail["mentions"].([]string); !slices.Equal(got, want) {
		t.Errorf("mentions = %v, want %v", got, want)
	}
	if len(s.Seeds) != 1 || s.Seeds[0].Component != (Ref{"PROMPT", cand.Manifest.PromptSHA256}) || !slices.Equal(s.Seeds[0].Mentions, want) {
		t.Errorf("seeds = %+v", s.Seeds)
	}
	if s.Scope != (Scope{"support-refund-agent", "1.3.0"}) {
		t.Errorf("scope = %+v", s.Scope)
	}
	var plus, minus int
	for _, l := range it.Detail["diff"].([]DiffLine) {
		switch l.Op {
		case "+":
			plus++
		case "-":
			minus++
		}
	}
	if plus == 0 || minus == 0 || !strings.Contains(it.Summary, "refund_payment") {
		t.Errorf("diff +%d -%d, summary %q", plus, minus, it.Summary)
	}
}

func TestWithoutStoredTextAPromptChangeIsKnownByItsHash(t *testing.T) {
	base, cand := demo(t, "1.2.4"), demo(t, "1.3.0")
	base.PromptText, cand.PromptText = nil, nil
	s := Compute(base, cand, nil, nil)
	it := s.Items[0]
	if it.Confidence != ConfidenceHashOnly || it.Detail["diff"] != nil || it.Detail["diff_unavailable"] == nil {
		t.Errorf("prompt item without text = %+v", it)
	}
	// Nothing is known about which tools the change touches: none is
	// named, so every tool counts as directly affected.
	if len(s.Seeds[0].Mentions) != 0 {
		t.Errorf("mentions without text: %v", s.Seeds[0].Mentions)
	}
}

func TestIdenticalVersionsHaveNoChanges(t *testing.T) {
	v := demo(t, "1.3.0")
	s := Compute(v, v, nil, nil)
	if len(s.Items) != 0 || len(s.Seeds) != 0 {
		t.Fatalf("%+v", s)
	}
	b, _ := json.Marshal(s)
	if !strings.Contains(string(b), `"items":[]`) || !strings.Contains(string(b), `"seeds":[]`) {
		t.Errorf("empty lists must be lists: %s", b)
	}
}

func TestLimitsAndModelParametersChangeTheWholeVersion(t *testing.T) {
	s := Compute(demo(t, "1.2.3"), demo(t, "1.2.4"), nil, nil)
	if got := kinds(s); !slices.Equal(got, []string{
		"prompt:" + demo(t, "1.2.4").Manifest.PromptSHA256 + ":modified", "limits:support-refund-agent:modified",
	}) {
		t.Fatalf("1.2.3 → 1.2.4: %v", got)
	}
	if got := s.Items[1].Detail["limits"].([]string); !slices.Equal(got, []string{"max_steps"}) {
		t.Errorf("limits = %v", got)
	}
	if !slices.Contains(seedsOf(s), "AGENT_VERSION:support-refund-agent@1.2.4:modified") {
		t.Errorf("seeds = %v", seedsOf(s))
	}
}

const toolsBase = `apiVersion: agenttwin.dev/v1
kind: Agent
metadata: {name: billing-agent, version: 1.0.0}
spec:
  instructions: Help with invoices.
  model: {provider: scripted, name: planner-v1, temperature: 0.2}
  retrieval:
    sources: [{name: billing-kb}]
  tools:
    - name: lookup_invoice
      risk: READ
    - name: legacy_export
      risk: READ
    - name: refund_invoice
      description: Refund an invoice.
      risk: WRITE_REVERSIBLE
      inputSchema:
        type: object
        required: [invoice_id]
        properties:
          invoice_id: {type: string}
          amount: {type: number, minimum: 0}
          currency: {type: string, enum: [USD, EUR]}
          note: {type: string}
  dependencies:
    - tool: refund_invoice
      dependsOn:
        - {kind: SERVICE, name: billing-api, relation: CALLS, criticality: HIGH}
        - {kind: QUEUE, name: refund-events, relation: PUBLISHES}
`

const toolsCandidate = `apiVersion: agenttwin.dev/v1
kind: Agent
metadata: {name: billing-agent, version: 1.1.0}
spec:
  instructions: Help with invoices.
  model: {provider: scripted, name: planner-v2, temperature: 0.7}
  retrieval:
    sources: [{name: billing-faq}]
  tools:
    - name: lookup_invoice
      risk: READ
    - name: refund_invoice
      description: Refund an invoice immediately.
      risk: WRITE_IRREVERSIBLE
      inputSchema:
        type: object
        required: [invoice_id, reason]
        properties:
          invoice_id: {type: string}
          amount: {type: integer, minimum: 0}
          currency: {type: string, enum: [USD]}
          reason: {type: string}
    - name: delete_customer
      risk: ADMIN
  dependencies:
    - tool: refund_invoice
      dependsOn:
        - {kind: SERVICE, name: billing-api, relation: CALLS, criticality: CRITICAL}
        - {kind: DATABASE, name: ledger, relation: WRITES, criticality: CRITICAL}
`

func TestToolModelRetrievalAndDependencyChanges(t *testing.T) {
	s := Compute(parse(t, toolsBase), parse(t, toolsCandidate), nil, nil)
	want := []string{
		"model:scripted/planner-v2:modified",
		"model_params:scripted/planner-v2:modified",
		"tool:delete_customer:added",
		"tool:legacy_export:removed",
		"tool:refund_invoice:modified",
		"retrieval_source:billing-faq:added",
		"retrieval_source:billing-kb:removed",
		"dependency:refund_invoice → DATABASE:ledger:added",
		"dependency:refund_invoice → QUEUE:refund-events:removed",
		"dependency:refund_invoice → SERVICE:billing-api:modified",
	}
	if got := kinds(s); !slices.Equal(got, want) {
		t.Fatalf("items:\n%v\nwant\n%v", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	byKey := map[string]Item{}
	for _, it := range s.Items {
		byKey[it.Kind+":"+it.Subject] = it
	}
	added := byKey["tool:delete_customer"]
	if added.Detail["new_privilege"] != true || added.Detail["risk"] != domain.RiskAdmin {
		t.Errorf("added tool = %+v", added)
	}
	mod := byKey["tool:refund_invoice"]
	risk := mod.Detail["risk"].(map[string]any)
	if risk["from"] != domain.RiskWriteReversible || risk["to"] != domain.RiskWriteIrreversible || risk["escalated"] != true || !mod.Breaking {
		t.Errorf("modified tool = %+v", mod)
	}
	if got := mod.Detail["aspects"].([]string); !slices.Equal(got, []string{"risk", "description", "schema"}) {
		t.Errorf("aspects = %v", got)
	}
	var changes []string
	for _, c := range mod.Detail["schema_changes"].([]SchemaChange) {
		changes = append(changes, fmt.Sprintf("%s %s %v", c.Path, c.Change, c.Breaking))
	}
	if !slices.Equal(changes, []string{
		"/properties/amount type_changed true",
		"/properties/currency enum_changed true",
		"/properties/note property_removed true",
		"/properties/reason required_added true",
	}) {
		t.Errorf("schema changes:\n%s", strings.Join(changes, "\n"))
	}
	if p := byKey["model_params:scripted/planner-v2"].Detail["parameters"].([]string); !slices.Equal(p, []string{"temperature"}) {
		t.Errorf("parameters = %v", p)
	}
	// Seeds: the new model, each changed tool (a dependency seeds its tool),
	// the retrieval sources and the version (parameters).
	for _, want := range []string{
		"MODEL:scripted/planner-v2:modified", "AGENT_VERSION:billing-agent@1.1.0:modified", "TOOL:delete_customer:added",
		"TOOL:legacy_export:removed", "TOOL:refund_invoice:modified", "RETRIEVAL_SOURCE:billing-faq:added",
		"RETRIEVAL_SOURCE:billing-kb:removed",
	} {
		if !slices.Contains(seedsOf(s), want) {
			t.Errorf("seeds %v miss %s", seedsOf(s), want)
		}
	}
	if n := strings.Count(strings.Join(seedsOf(s), " "), "TOOL:refund_invoice:modified"); n != 1 {
		t.Errorf("refund_invoice seeded %d times (its own change and three dependency changes merge)", n)
	}
}

func TestCodeChangesAreFileNamesOnly(t *testing.T) {
	base, cand := demo(t, "1.3.0"), demo(t, "1.3.0")
	a, b := "1111111", "2222222"
	base.CommitSHA, cand.CommitSHA = &a, &b
	s := Compute(base, cand, nil, nil)
	if got := kinds(s); !slices.Equal(got, []string{"code:2222222:modified"}) || s.Items[0].Confidence != ConfidenceFilenames {
		t.Fatalf("commit change: %v %+v", got, s.Items)
	}
	files := make([]string, 250)
	for i := range files {
		files[i] = fmt.Sprintf("src/file_%03d.py", i)
	}
	s = Compute(demo(t, "1.3.0"), demo(t, "1.3.0"), &Git{BaseCommit: "abc1234", CandidateCommit: "def5678", ChangedFiles: files}, nil)
	it := s.Items[0]
	if it.Detail["changed_file_count"] != 250 || len(it.Detail["changed_files"].([]string)) != maxFilesShown ||
		it.Detail["changed_files_truncated"] != true || !strings.Contains(it.Summary, "names only") {
		t.Errorf("git item = %+v", it.Detail)
	}
	if !slices.Equal(seedsOf(s), []string{"AGENT_VERSION:support-refund-agent@1.3.0:modified"}) {
		t.Errorf("seeds = %v", seedsOf(s))
	}
}

func TestDeclaredChanges(t *testing.T) {
	v := demo(t, "1.3.0")
	s := Compute(v, v, nil, []Declared{
		{Kind: "policy", Name: "refund-limit", Change: "modified", Summary: "limit lowered to 50"},
		{Kind: "dataset", Name: "refunds-core", Change: "added"},
	})
	if got := kinds(s); !slices.Equal(got, []string{"policy:refund-limit:modified", "dataset:refunds-core:added"}) {
		t.Fatalf("%v", got)
	}
	if s.Items[0].Confidence != ConfidenceDeclared || !slices.Equal(seedsOf(s), []string{"POLICY:refund-limit:modified", "DATASET:refunds-core:added"}) {
		t.Errorf("%+v %v", s.Items[0], seedsOf(s))
	}
}

func TestSecretsInAPromptDiffAreMasked(t *testing.T) {
	a := "Be helpful.\nUse the billing API."
	b := "Be helpful.\nUse the billing API with api_key: sk-live-abcdefghijklmnopqrstuvwxyz and mail ops@example.com."
	base, cand := demo(t, "1.3.0"), demo(t, "1.3.0")
	base.Manifest.PromptSHA256, cand.Manifest.PromptSHA256 = "aaaa", "bbbb"
	base.PromptText, cand.PromptText = &a, &b
	s := Compute(base, cand, nil, nil)
	out, _ := json.Marshal(s)
	for _, leak := range []string{"sk-live-abcdefghijklmnopqrstuvwxyz", "ops@example.com"} {
		if strings.Contains(string(out), leak) {
			t.Errorf("the change set shows %q", leak)
		}
	}
	if !strings.Contains(string(out), "REDACTED") {
		t.Errorf("nothing was masked: %s", out)
	}
}

func TestAPromptAddedOrRemoved(t *testing.T) {
	base, cand := demo(t, "1.3.0"), demo(t, "1.3.0")
	base.Manifest.PromptSHA256, base.PromptText = "", nil
	s := Compute(base, cand, nil, nil)
	if s.Items[0].Change != "added" || s.Seeds[0].Component.Kind != "PROMPT" || s.Seeds[0].Change != "added" {
		t.Errorf("added: %+v %+v", s.Items[0], s.Seeds)
	}
	s = Compute(demo(t, "1.3.0"), func() Version { v := demo(t, "1.3.0"); v.Manifest.PromptSHA256, v.PromptText = "", nil; return v }(), nil, nil)
	if s.Items[0].Change != "removed" || s.Seeds[0].Component != (Ref{"AGENT_VERSION", "support-refund-agent@1.3.0"}) {
		t.Errorf("removed: %+v %+v", s.Items[0], s.Seeds)
	}
}

func TestComputeIsDeterministic(t *testing.T) {
	first, _ := json.Marshal(Compute(parse(t, toolsBase), parse(t, toolsCandidate), nil, nil))
	for range 50 {
		again, _ := json.Marshal(Compute(parse(t, toolsBase), parse(t, toolsCandidate), nil, nil))
		if string(again) != string(first) {
			t.Fatal("the same versions gave two different change sets")
		}
	}
}

// Only the changed lines count, and a tool name is a whole word: "refund"
// is not mentioned by "refund_payment".
func TestMentionsAreWholeToolNamesInChangedLines(t *testing.T) {
	doc := func(version, instructions string) string {
		return `apiVersion: agenttwin.dev/v1
kind: Agent
metadata: {name: pay-agent, version: ` + version + `}
spec:
  instructions: "` + instructions + `"
  model: {provider: scripted, name: planner-v1}
  tools:
    - {name: refund, risk: READ}
    - {name: refund_payment, risk: WRITE_IRREVERSIBLE}
    - {name: send_email, risk: WRITE_REVERSIBLE}
    - {name: lookup_order, risk: READ}
`
	}
	base := parse(t, doc("1.0.0", `Always call lookup_order first.\nThen call REFUND_PAYMENT once.`))
	cand := parse(t, doc("1.1.0", `Always call lookup_order first.\nThen call Refund_Payment twice, then send_email.`))
	s := Compute(base, cand, nil, nil)
	if got := s.Seeds[0].Mentions; !slices.Equal(got, []string{"refund_payment", "send_email"}) {
		t.Errorf("mentions = %v (lookup_order is only in an unchanged line; refund is not refund_payment)", got)
	}
}

// A seed is the graph service's input: its summary and mentions fit what
// that service accepts, however many tools a prompt names.
func TestSeedsFitTheGraphServiceBounds(t *testing.T) {
	var tools, names []string
	for i := range 150 {
		name := fmt.Sprintf("tool_%03d", i)
		tools = append(tools, "    - {name: "+name+", risk: READ}")
		names = append(names, name)
	}
	doc := func(version, instructions string) string {
		return `apiVersion: agenttwin.dev/v1
kind: Agent
metadata: {name: many-tools, version: ` + version + `}
spec:
  instructions: "` + instructions + `"
  model: {provider: scripted, name: planner-v1}
  tools:
` + strings.Join(tools, "\n") + "\n"
	}
	s := Compute(parse(t, doc("1.0.0", "Be brief.")), parse(t, doc("1.1.0", "Use "+strings.Join(names, " and ")+".")), nil, nil)
	if len(s.Items[0].Detail["mentions"].([]string)) != 150 {
		t.Errorf("the item lists %d mentions, want all 150", len(s.Items[0].Detail["mentions"].([]string)))
	}
	seed := s.Seeds[0]
	if len(seed.Mentions) != MaxSeedMentions {
		t.Errorf("seed mentions = %d, bound %d", len(seed.Mentions), MaxSeedMentions)
	}
	if n := len([]rune(seed.Summary)); n != MaxSeedSummary || !strings.HasSuffix(seed.Summary, "…") {
		t.Errorf("seed summary is %d runes, bound %d: %q", n, MaxSeedSummary, seed.Summary)
	}
}
