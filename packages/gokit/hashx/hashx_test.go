package hashx

import (
	"encoding/json"
	"os"
	"testing"
)

type fixture struct {
	Cases []struct {
		Name      string          `json:"name"`
		Input     json.RawMessage `json:"input"`
		Canonical string          `json:"canonical"`
	} `json:"cases"`
}

func TestCanonicalJSONMatchesSharedFixture(t *testing.T) {
	raw, err := os.ReadFile("../../contracts/fixtures/canonical-json.json")
	if err != nil {
		t.Fatal(err)
	}
	var fx fixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	if len(fx.Cases) == 0 {
		t.Fatal("fixture has no cases")
	}
	for _, c := range fx.Cases {
		t.Run(c.Name, func(t *testing.T) {
			got, err := CanonicalJSON(c.Input)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != c.Canonical {
				t.Fatalf("canonical mismatch\n got: %s\nwant: %s", got, c.Canonical)
			}
		})
	}
}

func TestHashIsIndependentOfKeyOrder(t *testing.T) {
	a := map[string]any{"x": 1, "y": map[string]any{"b": 2, "a": 1}}
	b := map[string]any{"y": map[string]any{"a": 1, "b": 2}, "x": 1}
	if MustOf(a) != MustOf(b) {
		t.Fatal("hash must not depend on key order")
	}
	if MustOf(a) == MustOf(map[string]any{"x": 2}) {
		t.Fatal("different content must hash differently")
	}
	if len(MustOf(a)) != 64 {
		t.Fatal("expected hex sha256")
	}
}

func FuzzCanonicalJSONIsIdempotent(f *testing.F) {
	f.Add(`{"b":1,"a":[1,2,{"z":null}]}`)
	f.Add(`[1.5, "x", true]`)
	f.Add(`"plain"`)
	f.Add(`-0.0`)
	f.Add(`[5.944963824391085e+20, 1e21, 0.0001]`)
	f.Fuzz(func(t *testing.T, s string) {
		var v any
		if json.Unmarshal([]byte(s), &v) != nil {
			return
		}
		once, err := CanonicalJSON(v)
		if err != nil {
			return // non-finite etc.
		}
		var again any
		if err := json.Unmarshal(once, &again); err != nil {
			t.Fatalf("canonical output is not valid JSON: %s", once)
		}
		twice, err := CanonicalJSON(again)
		if err != nil {
			t.Fatal(err)
		}
		if string(once) != string(twice) {
			t.Fatalf("not idempotent:\n%s\n%s", once, twice)
		}
	})
}

// FuzzCanonicalJSONRawIsIdempotent feeds raw JSON text, so number literals
// reach the canonicalizer exactly as written (as they do for manifests and
// request bodies), e.g. "-0.0" or "5.944963824391085e+20".
func FuzzCanonicalJSONRawIsIdempotent(f *testing.F) {
	f.Add(`-0.0`)
	f.Add(`{"a":-0.0,"b":0.0,"c":1e21,"d":5.944963824391085e+20}`)
	f.Add(`[1.0, 2.50, 1E-7, 123456789012345678901234]`)
	f.Fuzz(func(t *testing.T, s string) {
		if !json.Valid([]byte(s)) {
			return
		}
		once, err := CanonicalJSON(json.RawMessage(s))
		if err != nil {
			return
		}
		twice, err := CanonicalJSON(json.RawMessage(once))
		if err != nil {
			t.Fatal(err)
		}
		if string(once) != string(twice) {
			t.Fatalf("not idempotent:\n%s\n%s", once, twice)
		}
	})
}
