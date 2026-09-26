package db

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"syscall"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func TestIsUnavailable(t *testing.T) {
	refused := &net.OpError{Op: "dial", Net: "tcp", Err: syscall.ECONNREFUSED}
	for _, c := range []struct {
		name string
		err  error
		want bool
	}{
		{"cannot connect", fmt.Errorf("begin tx: %w", &pgconn.ConnectError{}), true},
		{"connection refused", fmt.Errorf("query: %w", refused), true},
		{"connection dropped mid-query", io.ErrUnexpectedEOF, true},
		{"connection failure", &pgconn.PgError{Code: "08006"}, true},
		{"server shutting down", &pgconn.PgError{Code: "57P01"}, true},
		{"server crashed", &pgconn.PgError{Code: "57P02"}, true},
		{"server starting up", &pgconn.PgError{Code: "57P03"}, true},
		{"too many connections", fmt.Errorf("acquire: %w", &pgconn.PgError{Code: "53300"}), true},

		{"unique violation", &pgconn.PgError{Code: "23505"}, false},
		{"undefined table", &pgconn.PgError{Code: "42P01"}, false},
		{"query canceled", &pgconn.PgError{Code: "57014"}, false},
		{"no rows", pgx.ErrNoRows, false},
		{"the caller's deadline", fmt.Errorf("query: %w", context.DeadlineExceeded), false},
		{"the caller left", context.Canceled, false},
		{"a bug", errors.New("nil map"), false},
		{"nothing", nil, false},
	} {
		if got := IsUnavailable(c.err); got != c.want {
			t.Errorf("%s: IsUnavailable(%v) = %v, want %v", c.name, c.err, got, c.want)
		}
	}
}
