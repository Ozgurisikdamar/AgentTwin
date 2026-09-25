package content

import (
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
)

func span(redacted bool) *model.Span {
	return &model.Span{
		Resource:      model.Resource{ContentRedacted: redacted},
		StatusMessage: "failed for jane@example.com with Bearer abcdefgh12345678",
		Content: &model.Content{
			Input:  "refund to jane@example.com, key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",
			Output: strings.Repeat("x", 20000),
		},
		Attrs: model.Attrs{Extra: map[string]any{"note": "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl", "n": int64(3)}},
	}
}

func TestOffDropsAllContent(t *testing.T) {
	s := span(true)
	Apply(ForMode(Off), s)
	if s.Content != nil || !s.ContentDropped {
		t.Fatal("off mode must drop content")
	}
	if strings.Contains(s.StatusMessage, "jane@") || strings.Contains(s.StatusMessage, "abcdefgh12345678") {
		t.Fatalf("status message not sanitized: %q", s.StatusMessage)
	}
	if strings.Contains(s.Attrs.Extra["note"].(string), "eyJ") || s.Attrs.Extra["n"] != int64(3) {
		t.Fatalf("extra: %v", s.Attrs.Extra)
	}
}

func TestRedactedRequiresClientSideRedaction(t *testing.T) {
	s := span(false)
	Apply(ForMode(Redacted), s)
	if s.Content != nil || !s.ContentDropped {
		t.Fatal("redacted mode must drop content the SDK did not redact")
	}
	s = span(true)
	Apply(ForMode(Redacted), s)
	if s.Content == nil || strings.Contains(s.Content.Input, "jane@") || strings.Contains(s.Content.Input, "sk-ant") {
		t.Fatalf("redacted mode must keep SDK-redacted content and mask leftovers: %+v", s.Content)
	}
	if len(s.Content.Output) > 16<<10 || !s.Truncated {
		t.Fatalf("content must be truncated: len=%d truncated=%v", len(s.Content.Output), s.Truncated)
	}
}

func TestFullKeepsPIIButNeverSecrets(t *testing.T) {
	s := span(false)
	Apply(ForMode(Full), s)
	if s.Content == nil || !strings.Contains(s.Content.Input, "jane@example.com") {
		t.Fatal("full mode keeps personal data the project chose to capture")
	}
	if strings.Contains(s.Content.Input, "sk-ant") {
		t.Fatal("secrets are masked in every mode")
	}
}

func TestTheRequestContextIsContent(t *testing.T) {
	withContext := func(redacted bool) *model.Span {
		return &model.Span{
			Resource: model.Resource{ContentRedacted: redacted},
			Content:  &model.Content{InputContext: `{"tenant":"demo-co","contact":"jane@example.com"}`},
		}
	}
	// A context alone is content: off drops it.
	s := withContext(true)
	Apply(ForMode(Off), s)
	if s.Content != nil || !s.ContentDropped {
		t.Fatal("off mode must drop the request context")
	}
	// Redacted: kept, and masked again on the server.
	s = withContext(true)
	Apply(ForMode(Redacted), s)
	if s.Content == nil || s.Content.InputContext != `{"tenant":"demo-co","contact":"[REDACTED:email]"}` {
		t.Fatalf("redacted mode must mask the request context: %+v", s.Content)
	}
}
