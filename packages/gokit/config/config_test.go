package config

import (
	"strings"
	"testing"
	"time"
)

func lookupFrom(m map[string]string) func(string) (string, bool) {
	return func(k string) (string, bool) {
		v, ok := m[k]
		return v, ok
	}
}

func TestLoaderDefaultsAndParsing(t *testing.T) {
	l := NewWithLookup(lookupFrom(map[string]string{
		"PORT":    "8080",
		"RATIO":   "0.5",
		"ENABLED": "yes",
		"TIMEOUT": "3s",
		"HOSTS":   " a.example , ,b.example ",
		"MODE":    "dev",
	}))
	if got := l.Int("PORT", 1, 1, 65535); got != 8080 {
		t.Fatalf("PORT = %d", got)
	}
	if got := l.Float("RATIO", 0, 0, 1); got != 0.5 {
		t.Fatalf("RATIO = %v", got)
	}
	if !l.Bool("ENABLED", false) {
		t.Fatal("ENABLED should be true")
	}
	if got := l.Duration("TIMEOUT", time.Second); got != 3*time.Second {
		t.Fatalf("TIMEOUT = %v", got)
	}
	if got := l.List("HOSTS", nil); len(got) != 2 || got[0] != "a.example" || got[1] != "b.example" {
		t.Fatalf("HOSTS = %#v", got)
	}
	if got := l.OneOf("MODE", "dev", "dev", "oidc"); got != "dev" {
		t.Fatalf("MODE = %q", got)
	}
	if got := l.String("MISSING", "fallback"); got != "fallback" {
		t.Fatalf("MISSING = %q", got)
	}
	if err := l.Err(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if l.Env() != Development {
		t.Fatalf("default env should be development, got %s", l.Env())
	}
}

func TestLoaderAccumulatesAllErrors(t *testing.T) {
	l := NewWithLookup(lookupFrom(map[string]string{
		"PORT":  "abc",
		"LIMIT": "9999",
		"FLAG":  "maybe",
		"DUR":   "-1s",
		"MODE":  "weird",
	}))
	l.Required("DATABASE_URL")
	l.Int("PORT", 1, 1, 65535)
	l.Int("LIMIT", 1, 1, 100)
	l.Bool("FLAG", false)
	l.Duration("DUR", time.Second)
	l.OneOf("MODE", "dev", "dev", "oidc")
	err := l.Err()
	if err == nil {
		t.Fatal("expected error")
	}
	for _, want := range []string{"DATABASE_URL: is required", "PORT: must be an integer", "LIMIT: must be between", "FLAG: must be a boolean", "DUR: must be a non-negative duration", "MODE: must be one of"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not mention %q", err, want)
		}
	}
}

func TestProductionRejectsPlaceholderSecrets(t *testing.T) {
	l := NewWithLookup(lookupFrom(map[string]string{
		"APP_ENV":      "production",
		"SHORT_SECRET": "abc",
		"DEV_SECRET":   "change-me-dev-only-secret-value-that-is-long",
		"GOOD_SECRET":  "3b7f0f8c2a9d4e61b5c0a7d9e2f41c6b8a3d5e7f",
	}))
	l.Secret("SHORT_SECRET", 32)
	l.Secret("DEV_SECRET", 32)
	l.Secret("GOOD_SECRET", 32)
	err := l.Err()
	if err == nil {
		t.Fatal("expected production secret validation errors")
	}
	if !strings.Contains(err.Error(), "SHORT_SECRET: must be at least 32 bytes") {
		t.Errorf("missing short-secret error: %v", err)
	}
	if !strings.Contains(err.Error(), "DEV_SECRET: uses a development placeholder") {
		t.Errorf("missing placeholder error: %v", err)
	}
	if strings.Contains(err.Error(), "GOOD_SECRET") {
		t.Errorf("good secret must pass: %v", err)
	}
}

func TestDevelopmentAcceptsPlaceholderSecrets(t *testing.T) {
	l := NewWithLookup(lookupFrom(map[string]string{"S": "change-me"}))
	l.Secret("S", 32)
	if err := l.Err(); err != nil {
		t.Fatalf("development should accept placeholders: %v", err)
	}
}

func TestInvalidAppEnv(t *testing.T) {
	l := NewWithLookup(lookupFrom(map[string]string{"APP_ENV": "staging-ish"}))
	if l.Err() == nil {
		t.Fatal("invalid APP_ENV must be reported")
	}
}
