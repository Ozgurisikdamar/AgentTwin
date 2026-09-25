package store

import (
	"context"
	"errors"
)

// IdempotencyRecord is a stored response for an Idempotency-Key.
type IdempotencyRecord struct {
	RequestHash string
	Status      *int
	Response    []byte
	ContentType *string
}

// ErrIdempotencyMismatch means the key was reused with a different request.
var ErrIdempotencyMismatch = errors.New("idempotency key reused with a different request")

// ErrIdempotencyInProgress means the original request is still executing.
var ErrIdempotencyInProgress = errors.New("request with this idempotency key is in progress")

// BeginIdempotent claims (principal,key). It returns a completed record to
// replay, nil when the caller should execute the request, or an error.
func (s *Store) BeginIdempotent(ctx context.Context, principal, key, method, path, requestHash string) (*IdempotencyRecord, error) {
	tag, err := s.Pool.Exec(ctx, `
		INSERT INTO control.idempotency_record (principal, key, method, path, request_hash)
		VALUES ($1, $2, $3, $4, $5) ON CONFLICT (principal, key) DO NOTHING`, principal, key, method, path, requestHash)
	if err != nil {
		return nil, err
	}
	if tag.RowsAffected() == 1 {
		return nil, nil
	}
	var rec IdempotencyRecord
	var recMethod, recPath string
	err = s.Pool.QueryRow(ctx, `SELECT method, path, request_hash, status, response, content_type FROM control.idempotency_record WHERE principal = $1 AND key = $2`, principal, key).
		Scan(&recMethod, &recPath, &rec.RequestHash, &rec.Status, &rec.Response, &rec.ContentType)
	if err != nil {
		return nil, mapErr(err)
	}
	if recMethod != method || recPath != path || rec.RequestHash != requestHash {
		return nil, ErrIdempotencyMismatch
	}
	if rec.Status == nil {
		return nil, ErrIdempotencyInProgress
	}
	return &rec, nil
}

// CompleteIdempotent stores the final response.
func (s *Store) CompleteIdempotent(ctx context.Context, principal, key string, status int, body []byte, contentType string) error {
	_, err := s.Pool.Exec(ctx, `UPDATE control.idempotency_record SET status = $3, response = $4, content_type = $5 WHERE principal = $1 AND key = $2`,
		principal, key, status, body, contentType)
	return err
}

// AbandonIdempotent removes an in-progress claim (server error: allow retry).
func (s *Store) AbandonIdempotent(ctx context.Context, principal, key string) error {
	_, err := s.Pool.Exec(ctx, `DELETE FROM control.idempotency_record WHERE principal = $1 AND key = $2 AND status IS NULL`, principal, key)
	return err
}

// PurgeIdempotency deletes records older than the retention window (24h).
func (s *Store) PurgeIdempotency(ctx context.Context) (int64, error) {
	tag, err := s.Pool.Exec(ctx, `DELETE FROM control.idempotency_record WHERE created_at < now() - interval '24 hours'`)
	return tag.RowsAffected(), err
}
