// Package config loads typed configuration from environment variables.
//
// Every service builds its configuration through a Loader so that all problems
// are reported at once ("missing X, invalid Y") instead of failing on the first
// one, and so that production refuses the insecure development defaults that are
// convenient locally.
package config

import (
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Environment is the deployment environment of a process.
type Environment string

const (
	Development Environment = "development"
	Test        Environment = "test"
	Production  Environment = "production"
)

// devSecretPrefix marks placeholder secrets shipped in .env.example. Production
// startup rejects them so a copy-pasted example file can never reach prod.
const devSecretPrefix = "change-me"

// Loader accumulates configuration errors while reading variables.
type Loader struct {
	lookup func(string) (string, bool)
	errs   []string
	env    Environment
}

// New returns a Loader reading from the process environment.
func New() *Loader { return NewWithLookup(os.LookupEnv) }

// NewWithLookup returns a Loader using a custom lookup (tests).
func NewWithLookup(lookup func(string) (string, bool)) *Loader {
	l := &Loader{lookup: lookup}
	raw := strings.ToLower(l.String("APP_ENV", string(Development)))
	switch Environment(raw) {
	case Development, Test, Production:
		l.env = Environment(raw)
	default:
		l.fail("APP_ENV", fmt.Sprintf("must be one of development|test|production, got %q", raw))
		l.env = Development
	}
	return l
}

// Env returns the resolved deployment environment.
func (l *Loader) Env() Environment { return l.env }

func (l *Loader) fail(key, msg string) {
	l.errs = append(l.errs, fmt.Sprintf("%s: %s", key, msg))
}

func (l *Loader) get(key string) (string, bool) {
	v, ok := l.lookup(key)
	if !ok {
		return "", false
	}
	v = strings.TrimSpace(v)
	if v == "" {
		return "", false
	}
	return v, true
}

// String returns the variable or def when unset.
func (l *Loader) String(key, def string) string {
	if v, ok := l.get(key); ok {
		return v
	}
	return def
}

// Required returns the variable or records an error when it is missing.
func (l *Loader) Required(key string) string {
	v, ok := l.get(key)
	if !ok {
		l.fail(key, "is required")
	}
	return v
}

// Secret returns a required secret. In production the value must be at least
// minLen bytes and must not be a development placeholder.
func (l *Loader) Secret(key string, minLen int) string {
	v := l.Required(key)
	if v == "" {
		return v
	}
	if l.env == Production {
		if strings.HasPrefix(strings.ToLower(v), devSecretPrefix) {
			l.fail(key, "uses a development placeholder value; set a real secret in production")
		}
		if len(v) < minLen {
			l.fail(key, fmt.Sprintf("must be at least %d bytes in production", minLen))
		}
	}
	return v
}

// Int returns an integer variable with bounds checking.
func (l *Loader) Int(key string, def, min, max int) int {
	v, ok := l.get(key)
	if !ok {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		l.fail(key, fmt.Sprintf("must be an integer, got %q", v))
		return def
	}
	if n < min || n > max {
		l.fail(key, fmt.Sprintf("must be between %d and %d, got %d", min, max, n))
		return def
	}
	return n
}

// Float returns a float variable with bounds checking.
func (l *Loader) Float(key string, def, min, max float64) float64 {
	v, ok := l.get(key)
	if !ok {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		l.fail(key, fmt.Sprintf("must be a number, got %q", v))
		return def
	}
	if f < min || f > max {
		l.fail(key, fmt.Sprintf("must be between %g and %g, got %g", min, max, f))
		return def
	}
	return f
}

// Bool parses true/false/1/0/yes/no.
func (l *Loader) Bool(key string, def bool) bool {
	v, ok := l.get(key)
	if !ok {
		return def
	}
	switch strings.ToLower(v) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	}
	l.fail(key, fmt.Sprintf("must be a boolean, got %q", v))
	return def
}

// Duration parses Go durations ("5s", "2m").
func (l *Loader) Duration(key string, def time.Duration) time.Duration {
	v, ok := l.get(key)
	if !ok {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil || d < 0 {
		l.fail(key, fmt.Sprintf("must be a non-negative duration like 5s, got %q", v))
		return def
	}
	return d
}

// List splits a comma-separated variable, trimming blanks.
func (l *Loader) List(key string, def []string) []string {
	v, ok := l.get(key)
	if !ok {
		return def
	}
	var out []string
	for _, p := range strings.Split(v, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// OneOf validates the variable against an allowed set.
func (l *Loader) OneOf(key, def string, allowed ...string) string {
	v := l.String(key, def)
	for _, a := range allowed {
		if v == a {
			return v
		}
	}
	l.fail(key, fmt.Sprintf("must be one of %s, got %q", strings.Join(allowed, "|"), v))
	return def
}

// Check records a custom validation error when cond is false.
func (l *Loader) Check(cond bool, key, msg string) {
	if !cond {
		l.fail(key, msg)
	}
}

// Err returns all accumulated problems as one error (sorted, deterministic).
func (l *Loader) Err() error {
	if len(l.errs) == 0 {
		return nil
	}
	errs := append([]string(nil), l.errs...)
	sort.Strings(errs)
	return errors.New("invalid configuration:\n  - " + strings.Join(errs, "\n  - "))
}
