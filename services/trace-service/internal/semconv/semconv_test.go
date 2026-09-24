package semconv

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/otlp"
)

func decodeFixture(t *testing.T, name string) otlp.Span {
	t.Helper()
	b, err := os.ReadFile("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	otlp.Now = func() time.Time { return time.Unix(1790000000, 0).Add(time.Hour) }
	res, err := otlp.Decode(b, "application/json", otlp.DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	if res.Rejected != 0 || len(res.Spans) != 1 {
		t.Fatalf("decode %s: %d spans, %d rejected %v", name, len(res.Spans), res.Rejected, res.Errors)
	}
	return res.Spans[0]
}

// TestGenerationsNormalizeIdentically is the backward-compatibility contract:
// the same logical model call written with the current and the legacy GenAI
// conventions yields the same internal span, differing only in the recorded
// semconv version.
func TestGenerationsNormalizeIdentically(t *testing.T) {
	current := Normalize(decodeFixture(t, "genai-v1.37.json"))
	legacy := Normalize(decodeFixture(t, "genai-legacy.json"))
	if current.SemconvVersion != "genai-v1.37" || legacy.SemconvVersion != "genai-legacy" {
		t.Fatalf("detected versions %q / %q", current.SemconvVersion, legacy.SemconvVersion)
	}
	current.SemconvVersion, legacy.SemconvVersion = "", ""
	cj, _ := json.MarshalIndent(current, "", " ")
	lj, _ := json.MarshalIndent(legacy, "", " ")
	if !reflect.DeepEqual(current, legacy) {
		t.Fatalf("normalized spans differ:\ncurrent: %s\nlegacy:  %s", cj, lj)
	}
	a := current.Attrs
	if current.Kind != model.KindModel || a.Provider != "anthropic" || *a.InputTokens != 1200 || *a.OutputTokens != 85 ||
		a.SessionID != "sess-42" || a.RequestModel != "claude-sonnet-5" || current.TraceID != "5b8efff798038103d269b633813fc60c" {
		t.Fatalf("unexpected normalization: %s", cj)
	}
	if current.Content == nil || current.Content.Input == "" || current.Content.Output == "" {
		t.Fatal("content candidates must be extracted from both generations")
	}
	if current.Attrs.Extra["custom.team"] != "payments" || len(current.Attrs.Extra) != 1 {
		t.Fatalf("extra must contain only unmapped non-content attributes: %v", current.Attrs.Extra)
	}
	if current.Resource.Environment != "production" || current.Resource.AgentVersion != "1.2.4" {
		t.Fatalf("resource: %+v", current.Resource)
	}
}

func TestContentNeverLeaksIntoExtra(t *testing.T) {
	sp := otlp.Span{TraceID: "0123456789abcdef0123456789abcdef", SpanID: "0123456789abcdef", Name: "x",
		Start: time.Unix(1790000000, 0), End: time.Unix(1790000001, 0),
		Attrs: map[string]any{
			"gen_ai.prompt.0.role": "user", "gen_ai.prompt.0.content": "my card is 4111",
			"gen_ai.completion.0.content": "ok", "llm.input_messages.0.message.content": "hidden",
			"input.value": "hidden too", "gen_ai.system": "openai",
		}}
	s := Normalize(sp)
	for k := range s.Attrs.Extra {
		if IsContentKey(k) {
			t.Fatalf("content key %q leaked into extra", k)
		}
	}
	if s.Content == nil || s.Content.Input != "user: my card is 4111" || s.Content.Output != "ok" {
		t.Fatalf("indexed legacy content: %+v", s.Content)
	}
}

func TestKindInference(t *testing.T) {
	base := func(attrs map[string]any, parent string) model.Kind {
		return Normalize(otlp.Span{TraceID: "0123456789abcdef0123456789abcdef", SpanID: "0123456789abcdef", ParentSpanID: parent,
			Name: "step", Start: time.Unix(1790000000, 0), End: time.Unix(1790000001, 0), Attrs: attrs}).Kind
	}
	cases := []struct {
		name   string
		attrs  map[string]any
		parent string
		want   model.Kind
	}{
		{"explicit kind wins", map[string]any{"agenttwin.span.kind": "retrieval", "gen_ai.tool.name": "x"}, "aaaaaaaaaaaaaaaa", model.KindRetrieval},
		{"execute_tool", map[string]any{"gen_ai.operation.name": "execute_tool"}, "aaaaaaaaaaaaaaaa", model.KindTool},
		{"tool name", map[string]any{"gen_ai.tool.name": "refund_payment"}, "aaaaaaaaaaaaaaaa", model.KindTool},
		{"policy decision", map[string]any{"agenttwin.policy.decision": "deny"}, "aaaaaaaaaaaaaaaa", model.KindPolicy},
		{"outcome", map[string]any{"agenttwin.outcome.status": "SUCCESS"}, "aaaaaaaaaaaaaaaa", model.KindOutcome},
		{"http client", map[string]any{"http.request.method": "POST"}, "aaaaaaaaaaaaaaaa", model.KindHTTP},
		{"legacy model", map[string]any{"gen_ai.system": "openai"}, "aaaaaaaaaaaaaaaa", model.KindModel},
		{"root is the agent", map[string]any{}, "", model.KindAgent},
		{"unknown child", map[string]any{"db.system": "postgresql"}, "aaaaaaaaaaaaaaaa", model.KindOther},
	}
	for _, c := range cases {
		if got := base(c.attrs, c.parent); got != c.want {
			t.Errorf("%s: kind %q, want %q", c.name, got, c.want)
		}
	}
}
