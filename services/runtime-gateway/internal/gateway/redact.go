package gateway

import (
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"sort"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/redact"
)

// sensitiveKey matches argument names whose values are never shown.
var sensitiveKey = regexp.MustCompile(`(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential|cvv|cvc|ssn|card[_-]?number|iban)`)

// MaxShownString bounds a string argument shown to people.
const MaxShownString = 1000

var redactor = redact.All()

// RedactArgs returns the arguments as people may see them: values of
// sensitive names replaced, secrets and personal data inside strings masked,
// long strings truncated. The input is not changed.
func RedactArgs(v any) any {
	return redactValue("", v)
}

func redactValue(key string, v any) any {
	if sensitiveKey.MatchString(key) && v != nil {
		return "[REDACTED:field]"
	}
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = redactValue(k, e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		// A list under a sensitive name was redacted whole above.
		for i, e := range x {
			out[i] = redactValue("", e)
		}
		return out
	case string:
		s, _ := redactor.String(x)
		s, _ = redact.Truncate(s, MaxShownString)
		return s
	default:
		return v
	}
}

// Change is an argument that differs between two calls.
type Change struct {
	// Path is the argument: amount, items[1].sku, customer.email.
	Path string `json:"path"`
	// Before and After are absent when the argument is absent on that side.
	Before any `json:"before,omitempty"`
	After  any `json:"after,omitempty"`
	// Added or Removed tell an absent argument from a null one.
	Added   bool `json:"added,omitempty"`
	Removed bool `json:"removed,omitempty"`
}

// DiffArgs lists what differs from before to after, by path, sorted.
// Objects are compared key by key, arrays element by element; numbers by
// value (1 and 1.0 are equal).
func DiffArgs(before, after any) []Change {
	var out []Change
	diff("", normalizeNumbers(before), normalizeNumbers(after), &out)
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out
}

func diff(path string, a, b any, out *[]Change) {
	am, aIsMap := a.(map[string]any)
	bm, bIsMap := b.(map[string]any)
	if aIsMap && bIsMap {
		for k, av := range am {
			p := join(path, k)
			if bv, ok := bm[k]; ok {
				diff(p, av, bv, out)
			} else {
				*out = append(*out, Change{Path: p, Before: av, Removed: true})
			}
		}
		for k, bv := range bm {
			if _, ok := am[k]; !ok {
				*out = append(*out, Change{Path: join(path, k), After: bv, Added: true})
			}
		}
		return
	}
	al, aIsList := a.([]any)
	bl, bIsList := b.([]any)
	if aIsList && bIsList {
		for i := 0; i < len(al) || i < len(bl); i++ {
			p := fmt.Sprintf("%s[%d]", path, i)
			switch {
			case i >= len(bl):
				*out = append(*out, Change{Path: p, Before: al[i], Removed: true})
			case i >= len(al):
				*out = append(*out, Change{Path: p, After: bl[i], Added: true})
			default:
				diff(p, al[i], bl[i], out)
			}
		}
		return
	}
	if !reflect.DeepEqual(a, b) {
		*out = append(*out, Change{Path: path, Before: a, After: b})
	}
}

func join(path, key string) string {
	if path == "" {
		return key
	}
	return path + "." + key
}

// normalizeNumbers makes numbers comparable whatever their Go type.
func normalizeNumbers(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = normalizeNumbers(e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = normalizeNumbers(e)
		}
		return out
	case json.Number:
		f, err := x.Float64()
		if err != nil {
			return x.String()
		}
		return f
	case int:
		return float64(x)
	case int64:
		return float64(x)
	default:
		return v
	}
}
