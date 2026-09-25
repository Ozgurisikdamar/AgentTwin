// Package textx keeps text PostgreSQL cannot store away from it: a NUL
// character (rejected by text and jsonb columns alike) or a byte sequence that
// is not UTF-8. Either one, written by a caller, made a query fail inside the
// database and the service answer 500; an API refuses such text with a 400
// (see httpx), and telemetry, which must not be refused whole for one bad
// attribute, is cleaned instead.
package textx

import (
	"strings"
	"unicode/utf8"
)

// Replacement stands in for a character that cannot be stored.
const Replacement = "�"

// Valid reports whether s is storable: valid UTF-8 without U+0000.
func Valid(s string) bool {
	return utf8.ValidString(s) && strings.IndexByte(s, 0) < 0
}

// Clean replaces NUL characters and invalid UTF-8 with U+FFFD. Valid text is
// returned unchanged (and without allocating).
func Clean(s string) string {
	if Valid(s) {
		return s
	}
	return strings.ReplaceAll(strings.ToValidUTF8(s, Replacement), "\x00", Replacement)
}

// CleanValue cleans every string of a decoded JSON-like value, map keys
// included. Maps and slices are cleaned in place; the value is returned for
// convenience.
func CleanValue(v any) any {
	switch x := v.(type) {
	case string:
		return Clean(x)
	case []any:
		for i := range x {
			x[i] = CleanValue(x[i])
		}
		return x
	case map[string]any:
		for k, e := range x {
			ck := Clean(k)
			if ck != k {
				delete(x, k)
				// A cleaned key may meet one that was already clean; the
				// first one wins, as a JSON decoder keeps one of two
				// duplicate keys.
				if _, dup := x[ck]; dup {
					continue
				}
			}
			x[ck] = CleanValue(e)
		}
		return x
	}
	return v
}

// ValueValid reports whether every string of a decoded JSON-like value, map
// keys included, is Valid.
func ValueValid(v any) bool {
	switch x := v.(type) {
	case string:
		return Valid(x)
	case []any:
		for _, e := range x {
			if !ValueValid(e) {
				return false
			}
		}
	case map[string]any:
		for k, e := range x {
			if !Valid(k) || !ValueValid(e) {
				return false
			}
		}
	}
	return true
}
