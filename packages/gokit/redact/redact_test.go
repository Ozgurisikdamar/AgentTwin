package redact

import (
	"encoding/json"
	"strings"
	"testing"
	"unicode/utf8"

	contracts "github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
)

type fixture struct {
	Cases []struct {
		Name     string `json:"name"`
		Mode     string `json:"mode"`
		Strategy string `json:"strategy"`
		Input    string `json:"input"`
		Output   string `json:"output"`
	} `json:"cases"`
}

// TestSharedRedactionFixture is the Go side of the cross-language contract:
// the Python and TypeScript SDKs run the same cases.
func TestSharedRedactionFixture(t *testing.T) {
	raw, err := contracts.Fixtures.ReadFile("fixtures/redaction.json")
	if err != nil {
		t.Fatal(err)
	}
	var fx fixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	if len(fx.Cases) < 10 {
		t.Fatalf("fixture too small: %d cases", len(fx.Cases))
	}
	for _, c := range fx.Cases {
		t.Run(c.Name, func(t *testing.T) {
			r := Secrets()
			if c.Mode == "all" {
				r = All()
			}
			if c.Strategy == "hash" {
				r.Strategy = Hash
			}
			got, changed := r.String(c.Input)
			if got != c.Output {
				t.Fatalf("\ninput:  %q\ngot:    %q\nwant:   %q", c.Input, got, c.Output)
			}
			if changed != (c.Input != c.Output) {
				t.Fatalf("changed=%v but input != output is %v", changed, c.Input != c.Output)
			}
		})
	}
}

func TestTruncateKeepsValidUTF8(t *testing.T) {
	s := strings.Repeat("ş", 100) // 2 bytes each
	out, cut := Truncate(s, 51)
	if !cut || !utf8.ValidString(out) || len(out) > 51 {
		t.Fatalf("truncate: cut=%v valid=%v len=%d", cut, utf8.ValidString(out), len(out))
	}
	if out2, cut2 := Truncate("short", 100); cut2 || out2 != "short" {
		t.Fatal("short strings must be untouched")
	}
}

func FuzzRedactNeverPanicsAndIsIdempotent(f *testing.F) {
	f.Add("mail jane@example.com and Bearer abcdefgh12345678")
	f.Add("4111 1111 1111 1111")
	f.Add("password=secret123 +1 415 555 2671")
	f.Fuzz(func(t *testing.T, s string) {
		r := All()
		once, _ := r.String(s)
		twice, changed := r.String(once)
		if changed || twice != once {
			t.Fatalf("redaction is not idempotent:\n1: %q\n2: %q", once, twice)
		}
	})
}
