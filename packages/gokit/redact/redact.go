// Package redact masks secrets and personal data in free text. The same rules
// are implemented by the Python and TypeScript SDKs (client-side redaction)
// and verified against the shared fixture
// packages/contracts/fixtures/redaction.json.
package redact

import (
	"crypto/sha256"
	"encoding/hex"
	"regexp"
	"strings"
)

// Strategy selects what happens to a match.
type Strategy string

const (
	// Mask replaces a match with [REDACTED:<kind>].
	Mask Strategy = "mask"
	// Hash replaces a match with [HASH:<kind>:<12 hex of sha256>] so equal
	// values stay comparable without revealing them.
	Hash Strategy = "hash"
)

// Rule is one detector.
type Rule struct {
	Kind    string
	Pattern *regexp.Regexp
	// Group selects the sub-match to replace (0 = whole match).
	Group int
	// Valid optionally filters matches (e.g. Luhn check for cards).
	Valid func(match string) bool
}

// SecretRules detect credentials. They apply in every capture mode.
var SecretRules = []Rule{
	{Kind: "private_key", Pattern: regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`)},
	{Kind: "jwt", Pattern: regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}`)},
	{Kind: "bearer", Pattern: regexp.MustCompile(`(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{8,})`), Group: 1},
	{Kind: "api_key", Pattern: regexp.MustCompile(`\b(?:atk_[a-z0-9]{8}_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})`)},
	{Kind: "credential", Pattern: regexp.MustCompile(`(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b["']?\s*[:=]\s*["']?([^\s"',;]{4,})`), Group: 1},
}

// PIIRules detect personal data. They apply in redacted mode.
var PIIRules = []Rule{
	{Kind: "email", Pattern: regexp.MustCompile(`\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b`)},
	{Kind: "card", Pattern: regexp.MustCompile(`\b(?:\d[ -]?){12,18}\d\b`), Valid: luhn},
	// Phones need an explicit shape (international "+", parenthesized area
	// code, or dash/dot separated NANP) so amounts and ids are not masked.
	{Kind: "phone", Pattern: regexp.MustCompile(`\+\d{1,3}(?:[\s.-]?\d{1,4}){2,5}\b|\(\d{3}\)\s?\d{3}[\s.-]\d{4}\b|\b\d{3}[.-]\d{3}[.-]\d{4}\b`), Valid: phoneDigits},
}

// Redactor applies rules with a strategy.
type Redactor struct {
	Rules    []Rule
	Strategy Strategy
}

// Secrets returns a redactor for credentials only.
func Secrets() *Redactor { return &Redactor{Rules: SecretRules, Strategy: Mask} }

// All returns a redactor for credentials and personal data.
func All() *Redactor {
	rules := append(append([]Rule{}, SecretRules...), PIIRules...)
	return &Redactor{Rules: rules, Strategy: Mask}
}

// WithCustom adds custom regular expressions (kind "custom").
func (r *Redactor) WithCustom(patterns ...*regexp.Regexp) *Redactor {
	out := &Redactor{Rules: append([]Rule{}, r.Rules...), Strategy: r.Strategy}
	for _, p := range patterns {
		out.Rules = append(out.Rules, Rule{Kind: "custom", Pattern: p})
	}
	return out
}

// markerPattern matches replacements produced by this package. Rules never
// match inside or across markers, which keeps redaction idempotent.
var markerPattern = regexp.MustCompile(`\[(?:REDACTED:[a-z_]+|HASH:[a-z_]+:[0-9a-f]{12})\]`)

// maxRounds bounds fixpoint iteration. Every changing round replaces
// unredacted text with markers, so the loop terminates well before this.
const maxRounds = 8

// String redacts s and reports whether anything changed. Rules are applied in
// order to the text outside existing markers, and the whole pass repeats until
// nothing changes, so String(String(x)) == String(x).
func (r *Redactor) String(s string) (string, bool) {
	changed := false
	for round := 0; round < maxRounds; round++ {
		out, c := r.pass(s)
		if !c {
			break
		}
		s, changed = out, true
	}
	return s, changed
}

func (r *Redactor) pass(s string) (string, bool) {
	changed := false
	for _, rule := range r.Rules {
		out, c := r.applyOutsideMarkers(rule, s)
		s = out
		changed = changed || c
	}
	return s, changed
}

func (r *Redactor) applyOutsideMarkers(rule Rule, s string) (string, bool) {
	locs := markerPattern.FindAllStringIndex(s, -1)
	if len(locs) == 0 {
		return r.applyRule(rule, s)
	}
	var b strings.Builder
	changed := false
	prev := 0
	for _, loc := range locs {
		seg, c := r.applyRule(rule, s[prev:loc[0]])
		b.WriteString(seg)
		b.WriteString(s[loc[0]:loc[1]])
		changed = changed || c
		prev = loc[1]
	}
	seg, c := r.applyRule(rule, s[prev:])
	b.WriteString(seg)
	return b.String(), changed || c
}

func (r *Redactor) applyRule(rule Rule, s string) (string, bool) {
	matches := rule.Pattern.FindAllStringSubmatchIndex(s, -1)
	if len(matches) == 0 {
		return s, false
	}
	var b strings.Builder
	changed := false
	prev := 0
	for _, m := range matches {
		// Replace exactly the selected sub-match span (never a textual search
		// for it, which could hit an earlier occurrence such as the key name).
		start, end := m[0], m[1]
		if rule.Group > 0 {
			if len(m) <= 2*rule.Group+1 || m[2*rule.Group] < 0 {
				continue
			}
			start, end = m[2*rule.Group], m[2*rule.Group+1]
		}
		target := s[start:end]
		if target == "" || (rule.Valid != nil && !rule.Valid(target)) {
			continue
		}
		b.WriteString(s[prev:start])
		b.WriteString(r.replacement(rule.Kind, target))
		prev = end
		changed = true
	}
	if !changed {
		return s, false
	}
	b.WriteString(s[prev:])
	return b.String(), true
}

func (r *Redactor) replacement(kind, value string) string {
	if r.Strategy == Hash {
		sum := sha256.Sum256([]byte(value))
		return "[HASH:" + kind + ":" + hex.EncodeToString(sum[:])[:12] + "]"
	}
	return "[REDACTED:" + kind + "]"
}

func luhn(s string) bool {
	digits := make([]int, 0, 19)
	for _, c := range s {
		if c >= '0' && c <= '9' {
			digits = append(digits, int(c-'0'))
		}
	}
	if len(digits) < 13 || len(digits) > 19 {
		return false
	}
	sum := 0
	double := false
	for i := len(digits) - 1; i >= 0; i-- {
		d := digits[i]
		if double {
			d *= 2
			if d > 9 {
				d -= 9
			}
		}
		sum += d
		double = !double
	}
	return sum%10 == 0
}

func phoneDigits(s string) bool {
	n := 0
	for _, c := range s {
		if c >= '0' && c <= '9' {
			n++
		}
	}
	return n >= 9 && n <= 15
}

// Truncate shortens s to at most max bytes on a rune boundary, appending a
// marker, and reports whether it did.
func Truncate(s string, max int) (string, bool) {
	if max <= 0 || len(s) <= max {
		return s, false
	}
	const marker = "…[truncated]"
	cut := max - len(marker)
	if cut < 0 {
		cut = 0
	}
	for cut > 0 && !utf8Start(s[cut]) {
		cut--
	}
	return s[:cut] + marker, true
}

func utf8Start(b byte) bool { return b&0xC0 != 0x80 }
