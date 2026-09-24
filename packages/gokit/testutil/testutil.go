// Package testutil provides real-infrastructure helpers for integration tests
// (ADR-0012): an isolated PostgreSQL database per test and a RabbitMQ URL.
package testutil

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/url"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// requireOrSkip fails when AGENTTWIN_REQUIRE_INTEGRATION=1 (so CI can never
// silently skip integration tests) and skips otherwise.
func requireOrSkip(t testing.TB, what string) {
	t.Helper()
	if os.Getenv("AGENTTWIN_REQUIRE_INTEGRATION") == "1" {
		t.Fatalf("%s is required for integration tests but not configured", what)
	}
	t.Skipf("%s not configured; skipping integration test", what)
}

// NewDatabase creates a fresh database on the server named by
// AGENTTWIN_TEST_DATABASE_URL and returns its URL. It is dropped on cleanup.
func NewDatabase(t testing.TB) string {
	t.Helper()
	base := os.Getenv("AGENTTWIN_TEST_DATABASE_URL")
	if base == "" {
		requireOrSkip(t, "AGENTTWIN_TEST_DATABASE_URL")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	admin, err := pgx.Connect(ctx, base)
	if err != nil {
		t.Fatalf("connect admin database: %v", err)
	}
	var b [6]byte
	_, _ = rand.Read(b[:])
	name := "at_test_" + hex.EncodeToString(b[:])
	if _, err := admin.Exec(ctx, "CREATE DATABASE "+pgx.Identifier{name}.Sanitize()); err != nil {
		_ = admin.Close(ctx)
		t.Fatalf("create database: %v", err)
	}
	_ = admin.Close(ctx)
	u, err := url.Parse(base)
	if err != nil {
		t.Fatalf("parse url: %v", err)
	}
	u.Path = "/" + name
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		c, err := pgx.Connect(ctx, base)
		if err != nil {
			return
		}
		defer func() { _ = c.Close(ctx) }()
		_, _ = c.Exec(ctx, "DROP DATABASE IF EXISTS "+pgx.Identifier{name}.Sanitize()+" WITH (FORCE)")
	})
	return u.String()
}

// AMQPURL returns AGENTTWIN_TEST_AMQP_URL or skips/fails.
func AMQPURL(t testing.TB) string {
	t.Helper()
	u := os.Getenv("AGENTTWIN_TEST_AMQP_URL")
	if u == "" {
		requireOrSkip(t, "AGENTTWIN_TEST_AMQP_URL")
	}
	return u
}
