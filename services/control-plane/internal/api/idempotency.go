package api

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"regexp"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

var idemKeyPattern = regexp.MustCompile(`^[A-Za-z0-9._:-]{8,128}$`)

// captureWriter records the response for idempotent replay.
type captureWriter struct {
	http.ResponseWriter
	status int
	buf    bytes.Buffer
}

func (c *captureWriter) WriteHeader(code int) {
	c.status = code
	c.ResponseWriter.WriteHeader(code)
}

func (c *captureWriter) Write(b []byte) (int, error) {
	if c.status == 0 {
		c.status = http.StatusOK
	}
	if c.buf.Len() < 4<<20 {
		c.buf.Write(b)
	}
	return c.ResponseWriter.Write(b)
}

func (c *captureWriter) Flush() {
	if f, ok := c.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Idempotency implements Idempotency-Key for every mutating request at the edge,
// including requests proxied to internal services. A replay returns the stored
// response; reusing a key for a different request is rejected.
func Idempotency(st *store.Store) httpx.Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			key := r.Header.Get("Idempotency-Key")
			if key == "" || r.Method == http.MethodGet || r.Method == http.MethodHead || r.Method == http.MethodOptions {
				next.ServeHTTP(w, r)
				return
			}
			if !idemKeyPattern.MatchString(key) {
				httpx.WriteError(w, r, httpx.Invalid("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 8-128 characters of [A-Za-z0-9._:-].", nil))
				return
			}
			p, ok := authn.FromContext(r.Context())
			if !ok {
				next.ServeHTTP(w, r)
				return
			}
			body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 16<<20))
			if err != nil {
				httpx.WriteError(w, r, httpx.ErrPayloadTooBig)
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
			h := sha256.New()
			h.Write([]byte(r.Method + "\n" + r.URL.RequestURI() + "\n"))
			h.Write(body)
			reqHash := hex.EncodeToString(h.Sum(nil))
			principal := p.OrgID + "/" + p.Actor
			rec, err := st.BeginIdempotent(r.Context(), principal, key, r.Method, r.URL.Path, reqHash)
			switch {
			case errors.Is(err, store.ErrIdempotencyMismatch):
				httpx.WriteError(w, r, httpx.NewError(http.StatusUnprocessableEntity, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used for a different request."))
				return
			case errors.Is(err, store.ErrIdempotencyInProgress):
				httpx.WriteError(w, r, httpx.NewError(http.StatusConflict, "IDEMPOTENCY_IN_PROGRESS", "The original request with this Idempotency-Key is still being processed. Retry shortly."))
				return
			case err != nil:
				httpx.WriteError(w, r, err)
				return
			}
			if rec != nil {
				if rec.ContentType != nil {
					w.Header().Set("Content-Type", *rec.ContentType)
				}
				w.Header().Set("Idempotent-Replayed", "true")
				w.WriteHeader(*rec.Status)
				_, _ = w.Write(rec.Response)
				return
			}
			cw := &captureWriter{ResponseWriter: w}
			next.ServeHTTP(cw, r)
			if cw.status >= 500 || cw.status == 0 {
				_ = st.AbandonIdempotent(r.Context(), principal, key)
				return
			}
			_ = st.CompleteIdempotent(r.Context(), principal, key, cw.status, cw.buf.Bytes(), w.Header().Get("Content-Type"))
		})
	}
}
