// Package httpx contains the HTTP conventions shared by all Go services:
// a consistent error envelope, bounded JSON decoding, request IDs, recovery,
// security headers, health endpoints and graceful shutdown.
package httpx

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
)

// DefaultMaxBody is the default request body limit (1 MiB). Endpoints that
// legitimately accept more (trace ingestion, imports) set their own limit.
const DefaultMaxBody int64 = 1 << 20

// Error is an API error rendered as
// {"error":{"code","message","details","request_id"}}.
type Error struct {
	Status  int            `json:"-"`
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details,omitempty"`
}

func (e *Error) Error() string { return e.Code + ": " + e.Message }

// NewError builds an API error.
func NewError(status int, code, message string) *Error {
	return &Error{Status: status, Code: code, Message: message}
}

// WithDetails returns a copy of e with details attached.
func (e *Error) WithDetails(d map[string]any) *Error {
	c := *e
	c.Details = d
	return &c
}

// Common errors.
var (
	ErrNotFound       = NewError(http.StatusNotFound, "NOT_FOUND", "The requested resource does not exist.")
	ErrUnauthorized   = NewError(http.StatusUnauthorized, "UNAUTHENTICATED", "Authentication is required.")
	ErrForbidden      = NewError(http.StatusForbidden, "FORBIDDEN", "You do not have permission to perform this action.")
	ErrConflict       = NewError(http.StatusConflict, "CONFLICT", "The request conflicts with the current state.")
	ErrPayloadTooBig  = NewError(http.StatusRequestEntityTooLarge, "PAYLOAD_TOO_LARGE", "The request body is too large.")
	ErrInvalidJSON    = NewError(http.StatusBadRequest, "INVALID_JSON", "The request body is not valid JSON.")
	ErrInternal       = NewError(http.StatusInternalServerError, "INTERNAL", "An internal error occurred. Use the request id when contacting support.")
	ErrUnavailable    = NewError(http.StatusServiceUnavailable, "UNAVAILABLE", "A dependency is temporarily unavailable. Retry later.")
	ErrRateLimited    = NewError(http.StatusTooManyRequests, "RATE_LIMITED", "Too many requests. Retry later.")
	ErrMethodNotAllow = NewError(http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "Method not allowed.")
)

// Invalid returns a 400 validation error with per-field details.
func Invalid(code, message string, details map[string]any) *Error {
	return &Error{Status: http.StatusBadRequest, Code: code, Message: message, Details: details}
}

type errorBody struct {
	Error struct {
		Code      string         `json:"code"`
		Message   string         `json:"message"`
		Details   map[string]any `json:"details,omitempty"`
		RequestID string         `json:"request_id"`
	} `json:"error"`
}

// WriteError renders err. Non-API errors become 500 without leaking internals.
func WriteError(w http.ResponseWriter, r *http.Request, err error) {
	var apiErr *Error
	if !errors.As(err, &apiErr) {
		var mbe *http.MaxBytesError
		switch {
		case errors.As(err, &mbe):
			apiErr = ErrPayloadTooBig
		case errors.Is(err, context.DeadlineExceeded):
			apiErr = NewError(http.StatusGatewayTimeout, "TIMEOUT", "The operation timed out.")
		case errors.Is(err, context.Canceled):
			apiErr = NewError(499, "CANCELLED", "The request was cancelled.")
		default:
			slog.ErrorContext(r.Context(), "unhandled error", "error", err.Error(), "path", r.URL.Path)
			apiErr = ErrInternal
		}
	}
	var body errorBody
	body.Error.Code = apiErr.Code
	body.Error.Message = apiErr.Message
	body.Error.Details = apiErr.Details
	body.Error.RequestID = logx.RequestID(r.Context())
	status := apiErr.Status
	if status == 0 {
		status = http.StatusInternalServerError
	}
	WriteJSON(w, status, body)
}

// WriteJSON writes v as JSON with the given status.
func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(true) // content may be rendered by clients; keep HTML escaped
	_ = enc.Encode(v)
}

// DecodeJSON decodes a JSON body into dst with a size limit and strict fields.
func DecodeJSON(w http.ResponseWriter, r *http.Request, dst any, maxBytes int64) error {
	if maxBytes <= 0 {
		maxBytes = DefaultMaxBody
	}
	ct := r.Header.Get("Content-Type")
	if ct != "" && !strings.HasPrefix(strings.ToLower(ct), "application/json") {
		return NewError(http.StatusUnsupportedMediaType, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.")
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			return ErrPayloadTooBig
		}
		if errors.Is(err, io.EOF) {
			return NewError(http.StatusBadRequest, "EMPTY_BODY", "A JSON request body is required.")
		}
		return ErrInvalidJSON.WithDetails(map[string]any{"reason": err.Error()})
	}
	if dec.More() {
		return ErrInvalidJSON.WithDetails(map[string]any{"reason": "multiple JSON values in body"})
	}
	return nil
}

// ReadLimited reads the entire body up to maxBytes.
func ReadLimited(w http.ResponseWriter, r *http.Request, maxBytes int64) ([]byte, error) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
	b, err := io.ReadAll(r.Body)
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			return nil, ErrPayloadTooBig
		}
		return nil, fmt.Errorf("read body: %w", err)
	}
	return b, nil
}

// HandlerFunc is an http handler that returns an error rendered by WriteError.
type HandlerFunc func(w http.ResponseWriter, r *http.Request) error

// Handle adapts a HandlerFunc to http.HandlerFunc.
func Handle(fn HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := fn(w, r); err != nil {
			WriteError(w, r, err)
		}
	}
}
