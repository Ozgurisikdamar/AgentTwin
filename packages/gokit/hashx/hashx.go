// Package hashx computes stable content identities (SHA-256 over canonical JSON).
//
// Canonical form: object keys sorted by UTF-8 code units, no insignificant
// whitespace, no HTML escaping, integral numbers below 1e21 without fraction or
// exponent, -0 and 0.0 written as 0, other numbers in the shortest round-trip
// representation, non-finite numbers rejected. The Python SDK
// (agenttwin.hashing) follows the same rules; both are tested against
// packages/contracts/fixtures/canonical-json.json.
package hashx

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
)

// CanonicalJSON returns the canonical JSON encoding of v.
func CanonicalJSON(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("canonical json: marshal: %w", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var generic any
	if err := dec.Decode(&generic); err != nil {
		return nil, fmt.Errorf("canonical json: decode: %w", err)
	}
	var buf bytes.Buffer
	if err := writeCanonical(&buf, generic); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeCanonical(buf *bytes.Buffer, v any) error {
	switch t := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if t {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case json.Number:
		s, err := canonicalNumber(t)
		if err != nil {
			return err
		}
		buf.WriteString(s)
	case string:
		writeString(buf, t)
	case []any:
		buf.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonical(buf, e); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeString(buf, k)
			buf.WriteByte(':')
			if err := writeCanonical(buf, t[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		return fmt.Errorf("canonical json: unsupported type %T", v)
	}
	return nil
}

func canonicalNumber(n json.Number) (string, error) {
	if i, err := strconv.ParseInt(string(n), 10, 64); err == nil {
		return strconv.FormatInt(i, 10), nil
	}
	f, err := strconv.ParseFloat(string(n), 64)
	if err != nil {
		return "", fmt.Errorf("canonical json: bad number %q: %w", n, err)
	}
	if math.IsInf(f, 0) || math.IsNaN(f) {
		return "", fmt.Errorf("canonical json: non-finite number %q", n)
	}
	if f == 0 {
		// -0 and 0.0 denote the same value as 0; a single form keeps the
		// encoding a fixed point (canonical(canonical(x)) == canonical(x)).
		return "0", nil
	}
	if f == math.Trunc(f) && math.Abs(f) < 1e21 {
		return strconv.FormatFloat(f, 'f', -1, 64), nil
	}
	return strconv.FormatFloat(f, 'g', -1, 64), nil
}

func writeString(buf *bytes.Buffer, s string) {
	enc := json.NewEncoder(buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s) // strings always encode
	// Encoder appends a newline; remove it.
	buf.Truncate(buf.Len() - 1)
}

// SHA256Hex returns the hex SHA-256 of b.
func SHA256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Of returns the SHA-256 hex digest of the canonical JSON form of v.
func Of(v any) (string, error) {
	b, err := CanonicalJSON(v)
	if err != nil {
		return "", err
	}
	return SHA256Hex(b), nil
}

// MustOf is Of for values that are known to be JSON-encodable (panics otherwise).
func MustOf(v any) string {
	h, err := Of(v)
	if err != nil {
		panic(err)
	}
	return h
}
