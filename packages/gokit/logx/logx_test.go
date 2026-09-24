package logx

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
)

func TestLoggerRedactsSensitiveAttributesAndAddsRequestID(t *testing.T) {
	var buf bytes.Buffer
	log := New("test-svc", slog.LevelDebug, &buf)
	ctx := WithRequestID(context.Background(), "req-123")
	log.InfoContext(ctx, "hello", "api_key", "atk_abc_secret", "Authorization", "Bearer x", "user", "alice", "db_password", "p")
	var rec map[string]any
	if err := json.Unmarshal(buf.Bytes(), &rec); err != nil {
		t.Fatalf("not json: %v (%s)", err, buf.String())
	}
	if rec["service"] != "test-svc" || rec["request_id"] != "req-123" {
		t.Fatalf("missing correlation fields: %v", rec)
	}
	for _, k := range []string{"api_key", "Authorization", "db_password"} {
		if rec[k] != "[REDACTED]" {
			t.Errorf("%s not redacted: %v", k, rec[k])
		}
	}
	if rec["user"] != "alice" {
		t.Errorf("non-sensitive attr changed: %v", rec["user"])
	}
	if strings.Contains(buf.String(), "atk_abc_secret") {
		t.Fatal("secret leaked into log output")
	}
}

func TestParseLevel(t *testing.T) {
	cases := map[string]slog.Level{"debug": slog.LevelDebug, "WARN": slog.LevelWarn, "error": slog.LevelError, "": slog.LevelInfo, "bogus": slog.LevelInfo}
	for in, want := range cases {
		if got := ParseLevel(in); got != want {
			t.Errorf("ParseLevel(%q)=%v want %v", in, got, want)
		}
	}
}
