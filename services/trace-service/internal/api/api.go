// Package api is the trace-service HTTP layer: OTLP ingestion (API key auth)
// and the trace explorer, outcome and flag endpoints (internal JWT, reached
// through the control-plane proxy).
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/prometheus/client_golang/prometheus"
	coltracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	spb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/proto"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/content"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/keys"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/otlp"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/semconv"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/summary"
)

// Producer is the event producer name.
const Producer = "trace-service"

// Metrics are the ingestion counters.
type Metrics struct {
	Spans          *prometheus.CounterVec
	Requests       *prometheus.CounterVec
	Dropped        *prometheus.CounterVec
	Finalized      prometheus.Counter
	FinalizeErrors prometheus.Counter
	InFlight       prometheus.Gauge
	IngestTime     prometheus.Histogram
}

// NewMetrics registers ingestion metrics.
func NewMetrics(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		Spans: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "agenttwin_trace_spans_total",
			Help: "OTLP spans received by result (accepted, duplicate, rejected)."}, []string{"result"}),
		Requests: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "agenttwin_trace_ingest_requests_total",
			Help: "OTLP export requests by outcome."}, []string{"outcome"}),
		Dropped: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "agenttwin_trace_content_dropped_total",
			Help: "Spans whose content was dropped by the project capture policy."}, []string{"mode"}),
		Finalized: prometheus.NewCounter(prometheus.CounterOpts{Name: "agenttwin_trace_finalized_total",
			Help: "Traces finalized (summary computed)."}),
		FinalizeErrors: prometheus.NewCounter(prometheus.CounterOpts{Name: "agenttwin_trace_finalize_errors_total",
			Help: "Trace finalization attempts that failed (retried up to a bound until new spans arrive)."}),
		InFlight: prometheus.NewGauge(prometheus.GaugeOpts{Name: "agenttwin_trace_ingest_inflight",
			Help: "OTLP export requests being processed."}),
		IngestTime: prometheus.NewHistogram(prometheus.HistogramOpts{Name: "agenttwin_trace_ingest_duration_seconds",
			Help: "Time to decode, normalize and store one export request.", Buckets: prometheus.ExponentialBuckets(0.002, 2, 12)}),
	}
	reg.MustRegister(m.Spans, m.Requests, m.Dropped, m.Finalized, m.FinalizeErrors, m.InFlight, m.IngestTime)
	return m
}

// Server holds dependencies.
type Server struct {
	Store     *store.Store
	Keys      *keys.Verifier
	Log       *slog.Logger
	Metrics   *Metrics
	Limits    otlp.Limits
	MaxBody   int64
	Now       func() time.Time
	Tokens    *authn.TokenService
	semaphore chan struct{}
}

// Init sets defaults; maxConcurrent bounds concurrent ingest transactions
// (backpressure: excess requests get 503 + Retry-After and the collector retries).
func (s *Server) Init(maxConcurrent int) {
	if maxConcurrent <= 0 {
		maxConcurrent = 16
	}
	s.semaphore = make(chan struct{}, maxConcurrent)
	if s.Now == nil {
		s.Now = time.Now
	}
	if s.MaxBody == 0 {
		s.MaxBody = 16 << 20
	}
	if s.Limits.MaxSpans == 0 {
		s.Limits = otlp.DefaultLimits
	}
}

// Router is what routes are registered on (an http.ServeMux; a recorder in
// the test that holds them to the API contract).
type Router interface {
	Handle(pattern string, handler http.Handler)
	HandleFunc(pattern string, handler func(http.ResponseWriter, *http.Request))
}

// Routes registers all routes. Ingestion authenticates API keys itself; the
// query routes require an internal JWT.
func (s *Server) Routes(mux Router) {
	mux.HandleFunc("POST /v1/traces", s.ingest)
	internal := http.NewServeMux()
	s.QueryRoutes(internal)
	mux.Handle("/api/", httpx.Chain(internal, authn.RequireInternal(s.Tokens, "trace-service")))
}

// QueryRoutes registers the trace explorer, outcome and flag routes; Routes
// mounts them behind the internal token check.
func (s *Server) QueryRoutes(mux Router) {
	h := httpx.Handle
	mux.HandleFunc("GET /api/v1/traces", h(s.list))
	mux.HandleFunc("GET /api/v1/traces/facets", h(s.facets))
	mux.HandleFunc("GET /api/v1/traces/{trace_id}", h(s.detail))
	mux.HandleFunc("DELETE /api/v1/traces/{trace_id}", h(s.delete))
	mux.HandleFunc("POST /api/v1/traces/{trace_id}/outcome", h(s.recordOutcome))
	mux.HandleFunc("POST /api/v1/traces/{trace_id}/flag", h(s.flag))
	mux.HandleFunc("GET /api/v1/trace-stats", h(s.stats))
}

// ---------------------------------------------------------------- ingestion

func apiKeyFrom(r *http.Request) string {
	if k := strings.TrimSpace(r.Header.Get("X-AgentTwin-Api-Key")); k != "" {
		return k
	}
	return authn.BearerToken(r)
}

// grpcCodes are the google.rpc.Code values of the HTTP statuses ingestion
// answers (the mapping of google/rpc/code.proto); any other is UNKNOWN.
var grpcCodes = map[int]codes.Code{
	http.StatusBadRequest:            codes.InvalidArgument,
	http.StatusUnauthorized:          codes.Unauthenticated,
	http.StatusForbidden:             codes.PermissionDenied,
	http.StatusRequestEntityTooLarge: codes.ResourceExhausted,
	http.StatusUnsupportedMediaType:  codes.InvalidArgument,
	http.StatusTooManyRequests:       codes.ResourceExhausted,
	http.StatusInternalServerError:   codes.Internal,
	http.StatusServiceUnavailable:    codes.Unavailable,
}

// otlpError writes an OTLP/HTTP error: a google.rpc.Status in the encoding of
// the request — protobuf for a protobuf request, JSON otherwise. Statuses
// follow OTLP/HTTP retry semantics (503 is retryable, with Retry-After).
func otlpError(w http.ResponseWriter, r *http.Request, status int, msg string) {
	if status == http.StatusServiceUnavailable || status == http.StatusTooManyRequests {
		w.Header().Set("Retry-After", "2")
	}
	code, ok := grpcCodes[status]
	if !ok {
		code = codes.Unknown
	}
	if otlp.Protobuf(r.Header.Get("Content-Type")) {
		b, _ := proto.Marshal(&spb.Status{Code: int32(code), Message: msg}) //nolint:gosec // gRPC codes are 0..16
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(status)
		_, _ = w.Write(b)
		return
	}
	httpx.WriteJSON(w, status, map[string]any{"code": int(code), "message": msg})
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	httpx.SetRouteName(r, "POST /v1/traces")
	start := time.Now()
	outcome := "ok"
	defer func() {
		s.Metrics.Requests.WithLabelValues(outcome).Inc()
		s.Metrics.IngestTime.Observe(time.Since(start).Seconds())
	}()
	key := apiKeyFrom(r)
	if key == "" {
		outcome = "unauthenticated"
		otlpError(w, r, http.StatusUnauthorized, "missing API key (X-AgentTwin-Api-Key header)")
		return
	}
	info, err := s.Keys.Verify(r.Context(), key)
	switch {
	case errors.Is(err, keys.ErrInvalid):
		outcome = "unauthenticated"
		otlpError(w, r, http.StatusUnauthorized, "invalid, revoked or expired API key")
		return
	case err != nil:
		outcome = "auth_unavailable"
		s.Log.WarnContext(r.Context(), "api key verification unavailable", "error", err.Error())
		otlpError(w, r, http.StatusServiceUnavailable, "API key verification temporarily unavailable")
		return
	}
	if !info.Principal().Can(authn.PermTraceWrite) {
		outcome = "forbidden"
		otlpError(w, r, http.StatusForbidden, "API key lacks the traces:write scope")
		return
	}
	if err := otlp.CheckContentType(r.Header.Get("Content-Type")); err != nil {
		outcome = "unsupported"
		otlpError(w, r, http.StatusUnsupportedMediaType, err.Error())
		return
	}
	select {
	case s.semaphore <- struct{}{}:
		defer func() { <-s.semaphore }()
	default:
		outcome = "overloaded"
		otlpError(w, r, http.StatusServiceUnavailable, "trace ingestion is at capacity; retry")
		return
	}
	s.Metrics.InFlight.Inc()
	defer s.Metrics.InFlight.Dec()

	body, err := otlp.ReadBody(http.MaxBytesReader(w, r.Body, s.MaxBody), r.Header.Get("Content-Encoding"), s.Limits.MaxDecompressedBytes)
	if err == nil {
		var res otlp.Result
		if res, err = otlp.Decode(body, r.Header.Get("Content-Type"), s.Limits); err == nil {
			outcome = s.storeSpans(w, r, info, res)
			return
		}
	}
	status := http.StatusBadRequest
	outcome = "bad_request"
	var mbe *http.MaxBytesError
	switch {
	case errors.Is(err, otlp.ErrTooLarge), errors.As(err, &mbe):
		status, outcome = http.StatusRequestEntityTooLarge, "too_large"
	case errors.Is(err, otlp.ErrUnsupported):
		status, outcome = http.StatusUnsupportedMediaType, "unsupported"
	}
	otlpError(w, r, status, err.Error())
}

// storeSpans normalizes and stores the decoded spans and answers the export; it
// returns the request outcome for the metrics.
func (s *Server) storeSpans(w http.ResponseWriter, r *http.Request, info keys.Info, res otlp.Result) string {
	outcome := "ok"
	mode := content.Mode(info.ContentMode)
	if !content.Valid(mode) {
		mode = content.Off
	}
	policy := content.ForMode(mode)
	spans := make([]model.Span, 0, len(res.Spans))
	for _, raw := range res.Spans {
		sp := semconv.Normalize(raw)
		content.Apply(policy, &sp)
		if sp.ContentDropped {
			s.Metrics.Dropped.WithLabelValues(string(mode)).Inc()
		}
		spans = append(spans, sp)
	}
	tgt := store.Target{
		OrganizationID: info.OrganizationID, ProjectID: info.ProjectID, ContentMode: string(mode),
		TraceRetention: days(info.TraceRetentionDays, 30), ContentRetention: days(info.ContentRetentionDays, 7),
	}
	inserted, err := s.Store.Ingest(r.Context(), tgt, spans, s.Now())
	if err != nil {
		s.Log.ErrorContext(r.Context(), "trace ingest failed", "error", err.Error(), "spans", len(spans))
		otlpError(w, r, http.StatusServiceUnavailable, "trace storage temporarily unavailable; retry")
		return "store_error"
	}
	s.Metrics.Spans.WithLabelValues("accepted").Add(float64(inserted))
	s.Metrics.Spans.WithLabelValues("duplicate").Add(float64(len(spans) - inserted))
	s.Metrics.Spans.WithLabelValues("rejected").Add(float64(res.Rejected))
	if res.Rejected > 0 {
		outcome = "partial"
	}
	writeExportResponse(w, r.Header.Get("Content-Type"), res)
	return outcome
}

func days(n, def int) time.Duration {
	if n <= 0 {
		n = def
	}
	return time.Duration(n) * 24 * time.Hour
}

func writeExportResponse(w http.ResponseWriter, contentType string, res otlp.Result) {
	resp := &coltracepb.ExportTraceServiceResponse{}
	if res.Rejected > 0 {
		resp.PartialSuccess = &coltracepb.ExportTracePartialSuccess{
			RejectedSpans: int64(res.Rejected),
			ErrorMessage:  strings.Join(res.Errors, "; "),
		}
	}
	if otlp.Protobuf(contentType) {
		b, _ := proto.Marshal(resp)
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(b)
		return
	}
	out := map[string]any{}
	if res.Rejected > 0 {
		out["partialSuccess"] = map[string]any{"rejectedSpans": strconv.Itoa(res.Rejected), "errorMessage": strings.Join(res.Errors, "; ")}
	}
	httpx.WriteJSON(w, http.StatusOK, out)
}

// ---------------------------------------------------------------- queries

func scopeOf(p authn.Principal) store.Scope {
	return store.Scope{OrganizationID: p.OrgID, AllProjects: p.AllProjects, ProjectIDs: p.ProjectIDs}
}

var validTraceID = func(s string) bool {
	if len(s) != 32 {
		return false
	}
	for _, c := range s {
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func traceIDParam(r *http.Request) (string, error) {
	id := strings.ToLower(r.PathValue("trace_id"))
	if !validTraceID(id) {
		return "", httpx.Invalid("INVALID_PARAMETER", "trace_id must be 32 hex characters.", map[string]any{"field": "trace_id"})
	}
	return id, nil
}

// projectFilter reads the project_id filter of the explorer queries: a
// malformed id is a bad filter (400), a project outside the caller's access
// is not found (404) so its existence cannot be probed.
func projectFilter(r *http.Request, p authn.Principal) (string, error) {
	projectID := strings.ToLower(r.URL.Query().Get("project_id"))
	switch {
	case projectID == "":
		return "", nil
	case !ids.Valid(projectID):
		return "", httpx.Invalid("INVALID_FILTER", "project_id must be a UUID.", map[string]any{"field": "project_id"})
	case !p.CanAccessProject(projectID):
		return "", httpx.ErrNotFound
	}
	return projectID, nil
}

// oneOf checks an enumerated filter.
func oneOf(q url.Values, field string, values ...string) (string, error) {
	v := q.Get(field)
	if v == "" || slices.Contains(values, v) {
		return v, nil
	}
	return "", httpx.Invalid("INVALID_FILTER", field+" must be one of "+strings.Join(values, ", ")+".", map[string]any{"field": field})
}

func parseTime(q string, field string) (*time.Time, error) {
	if q == "" {
		return nil, nil
	}
	t, err := time.Parse(time.RFC3339, q)
	if err != nil {
		return nil, httpx.Invalid("INVALID_FILTER", field+" must be an RFC 3339 timestamp.", map[string]any{"field": field})
	}
	return &t, nil
}

func parseFloat(q, field string) (*float64, error) {
	if q == "" {
		return nil, nil
	}
	f, err := strconv.ParseFloat(q, 64)
	if err != nil || f < 0 {
		return nil, httpx.Invalid("INVALID_FILTER", field+" must be a non-negative number.", map[string]any{"field": field})
	}
	return &f, nil
}

func parseBool(q, field string) (*bool, error) {
	if q == "" {
		return nil, nil
	}
	b, err := strconv.ParseBool(q)
	if err != nil {
		return nil, httpx.Invalid("INVALID_FILTER", field+" must be true or false.", map[string]any{"field": field})
	}
	return &b, nil
}

func (s *Server) list(w http.ResponseWriter, r *http.Request) error {
	p, err := authn.Require(r, authn.PermRead)
	if err != nil {
		return err
	}
	q := r.URL.Query()
	f := store.Filter{
		Agent: q.Get("agent"), AgentVersion: q.Get("agent_version"),
		Environment: q.Get("environment"), Release: q.Get("release"), Model: q.Get("model"), Tool: q.Get("tool"),
		Signal: q.Get("signal"), PolicyDecision: q.Get("policy_decision"), SessionID: q.Get("session_id"),
		SimulationRunID: q.Get("simulation_run_id"),
	}
	if f.ProjectID, err = projectFilter(r, p); err != nil {
		return err
	}
	if f.Status, err = oneOf(q, "status", "OK", "ERROR", "UNSET"); err != nil {
		return err
	}
	if f.Outcome, err = oneOf(q, "outcome", "SUCCESS", "PARTIAL", "FAILURE", "UNKNOWN"); err != nil {
		return err
	}
	if f.Source, err = oneOf(q, "source", "production", "simulation", "replay", "eval", "test"); err != nil {
		return err
	}
	if f.From, err = parseTime(q.Get("from"), "from"); err != nil {
		return err
	}
	if f.To, err = parseTime(q.Get("to"), "to"); err != nil {
		return err
	}
	for _, n := range []struct {
		field string
		dst   **float64
	}{{"min_duration_ms", &f.MinDurationMS}, {"max_duration_ms", &f.MaxDurationMS}, {"min_cost_usd", &f.MinCostUSD}, {"max_cost_usd", &f.MaxCostUSD}} {
		if *n.dst, err = parseFloat(q.Get(n.field), n.field); err != nil {
			return err
		}
	}
	if f.HumanReviewed, err = parseBool(q.Get("human_reviewed"), "human_reviewed"); err != nil {
		return err
	}
	if f.Flagged, err = parseBool(q.Get("flagged"), "flagged"); err != nil {
		return err
	}
	if f.Limit, err = httpx.QueryInt(r, "limit", 50, 1, 200); err != nil {
		return err
	}
	if c := q.Get("cursor"); c != "" {
		cur, err := httpx.DecodeCursor(c)
		if err != nil {
			return err
		}
		f.CursorTS, f.CursorID = &cur.TS, cur.ID
	}
	items, err := s.Store.List(r.Context(), scopeOf(p), f)
	if err != nil {
		return err
	}
	var next *string // null on the last page
	if len(items) == f.Limit {
		last := items[len(items)-1]
		c := httpx.EncodeCursor(httpx.Cursor{TS: last.StartedAt, ID: last.TraceID})
		next = &c
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"items": items, "next_cursor": next})
	return nil
}

func (s *Server) lookup(r *http.Request, perm authn.Permission) (authn.Principal, store.Trace, error) {
	p, err := authn.Require(r, perm)
	if err != nil {
		return p, store.Trace{}, err
	}
	id, err := traceIDParam(r)
	if err != nil {
		return p, store.Trace{}, err
	}
	projectID := strings.ToLower(r.URL.Query().Get("project_id"))
	switch {
	case projectID != "" && !ids.Valid(projectID):
		return p, store.Trace{}, httpx.Invalid("INVALID_PARAMETER", "project_id must be a UUID.", map[string]any{"field": "project_id"})
	case projectID != "" && !p.CanAccessProject(projectID):
		return p, store.Trace{}, httpx.ErrNotFound
	}
	t, err := s.Store.Get(r.Context(), scopeOf(p), id, projectID)
	switch {
	case errors.Is(err, store.ErrNotFound):
		return p, t, httpx.ErrNotFound
	case errors.Is(err, store.ErrAmbiguous):
		return p, t, httpx.NewError(http.StatusConflict, "AMBIGUOUS_TRACE", "This trace id exists in several projects; pass project_id.")
	}
	return p, t, err
}

func (s *Server) detail(w http.ResponseWriter, r *http.Request) error {
	_, t, err := s.lookup(r, authn.PermRead)
	if err != nil {
		return err
	}
	spans, err := s.Store.Spans(r.Context(), t.ProjectID, t.TraceID)
	if err != nil {
		return err
	}
	outcome, err := s.Store.GetOutcome(r.Context(), s.Store.Pool, t.ProjectID, t.TraceID)
	if err != nil {
		return err
	}
	flags, err := s.Store.Flags(r.Context(), t.ProjectID, t.TraceID)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"trace": t, "spans": spans, "outcome": outcome, "flags": flags})
	return nil
}

func (s *Server) delete(w http.ResponseWriter, r *http.Request) error {
	p, t, err := s.lookup(r, authn.PermSettingsWrite)
	if err != nil {
		return err
	}
	err = db.WithTx(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(), `DELETE FROM trace.trace WHERE project_id = $1 AND trace_id = $2`, t.ProjectID, t.TraceID); err != nil {
			return err
		}
		return emitAudit(r.Context(), tx, p, t.ProjectID, "trace.deleted", "trace", t.TraceID, "privacy deletion request")
	})
	if err != nil {
		return err
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}

// OutcomeInput records an outcome (spec §15).
type OutcomeInput struct {
	Status             string          `json:"status"`
	BusinessOutcome    string          `json:"business_outcome"`
	Verified           bool            `json:"verified"`
	VerificationSource string          `json:"verification_source"`
	ClaimedStatus      string          `json:"claimed_status"`
	ExpectedState      json.RawMessage `json:"expected_state"`
	ActualState        json.RawMessage `json:"actual_state"`
	Notes              string          `json:"notes"`
}

var (
	outcomeStatuses     = summary.OutcomeStatuses
	verificationSources = summary.VerificationSources
)

func (s *Server) recordOutcome(w http.ResponseWriter, r *http.Request) error {
	// Humans record outcomes as reviewers; SDK/automation keys (outcome
	// callbacks) need traces:write.
	perm := authn.PermReviewWrite
	if caller, ok := authn.FromContext(r.Context()); ok && caller.Role == authn.RoleAPIKey {
		perm = authn.PermTraceWrite
	}
	p, t, err := s.lookup(r, perm)
	if err != nil {
		return err
	}
	var in OutcomeInput
	if err := httpx.DecodeJSON(w, r, &in, 256<<10); err != nil {
		return err
	}
	if !outcomeStatuses[in.Status] {
		return httpx.Invalid("INVALID_OUTCOME", "status must be SUCCESS, PARTIAL, FAILURE or UNKNOWN.", map[string]any{"field": "status"})
	}
	if in.ClaimedStatus != "" && !outcomeStatuses[in.ClaimedStatus] {
		return httpx.Invalid("INVALID_OUTCOME", "claimed_status must be SUCCESS, PARTIAL, FAILURE or UNKNOWN.", map[string]any{"field": "claimed_status"})
	}
	if in.VerificationSource == "" {
		in.VerificationSource = "unavailable"
		if p.Role != authn.RoleAPIKey && p.Role != authn.RoleService {
			in.VerificationSource = "human_review"
		}
	}
	if !verificationSources[in.VerificationSource] {
		return httpx.Invalid("INVALID_OUTCOME", "unknown verification_source.", map[string]any{"field": "verification_source"})
	}
	if in.Verified && in.VerificationSource == "unavailable" {
		return httpx.Invalid("INVALID_OUTCOME", "An outcome cannot be verified without a verification source; a final answer alone is not verification.", map[string]any{"field": "verified"})
	}
	if utf8.RuneCountInString(in.BusinessOutcome) > 200 {
		return httpx.Invalid("INVALID_OUTCOME", "business_outcome must be at most 200 characters.", map[string]any{"field": "business_outcome"})
	}
	if utf8.RuneCountInString(in.Notes) > 4000 {
		return httpx.Invalid("INVALID_OUTCOME", "notes must be at most 4000 characters.", map[string]any{"field": "notes"})
	}
	for _, st := range []struct {
		field string
		raw   json.RawMessage
	}{{"expected_state", in.ExpectedState}, {"actual_state", in.ActualState}} {
		if raw := bytes.TrimSpace(st.raw); len(raw) > 0 && raw[0] != '{' && string(raw) != "null" {
			return httpx.Invalid("INVALID_OUTCOME", st.field+" must be an object.", map[string]any{"field": st.field})
		}
	}
	contradiction := summary.Contradiction(in.ClaimedStatus, in.Status, in.Verified)
	humanReview := in.VerificationSource == "human_review"
	err = db.WithTx(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(), `
			INSERT INTO trace.outcome (project_id, trace_id, status, business_outcome, verified, verification_source, claimed_status,
				contradiction, expected_state, actual_state, notes, source, recorded_by, emitted_at)
			VALUES ($1, $2, $3, NULLIF($4, ''), $5, $6, NULLIF($7, ''), $8, $9, $10, NULLIF($11, ''), 'api', $12, now())
			ON CONFLICT (project_id, trace_id) DO UPDATE SET status = EXCLUDED.status, business_outcome = EXCLUDED.business_outcome,
				verified = EXCLUDED.verified, verification_source = EXCLUDED.verification_source, claimed_status = EXCLUDED.claimed_status,
				contradiction = EXCLUDED.contradiction, expected_state = EXCLUDED.expected_state, actual_state = EXCLUDED.actual_state,
				notes = EXCLUDED.notes, source = 'api', recorded_by = EXCLUDED.recorded_by, recorded_at = now(), emitted_at = now()`,
			t.ProjectID, t.TraceID, in.Status, in.BusinessOutcome, in.Verified, in.VerificationSource, in.ClaimedStatus,
			contradiction, nullJSON(in.ExpectedState), nullJSON(in.ActualState), in.Notes, p.Actor); err != nil {
			return err
		}
		if _, err := tx.Exec(r.Context(), `
			UPDATE trace.trace SET outcome_status = $3, outcome_verified = $4, human_reviewed = human_reviewed OR $5,
				signals = CASE WHEN $6 AND NOT ('contradiction' = ANY(signals)) THEN array_append(signals, 'contradiction') ELSE signals END,
				summary = jsonb_set(jsonb_set(summary, '{outcome}', to_jsonb($3::text)), '{outcome_verified}', to_jsonb($4::boolean))
			WHERE project_id = $1 AND trace_id = $2`, t.ProjectID, t.TraceID, in.Status, in.Verified, humanReview, contradiction); err != nil {
			return err
		}
		env, err := events.New("trace.outcome_recorded.v1", Producer, p.OrgID, t.ProjectID, logx.RequestID(r.Context()), "", map[string]any{
			"trace_id": t.TraceID, "status": in.Status, "business_outcome": nilIfEmpty(in.BusinessOutcome), "verified": in.Verified,
			"verification_source": in.VerificationSource, "contradiction": contradiction, "source": "api", "recorded_by": p.Actor,
		})
		if err != nil {
			return err
		}
		return events.WriteOutbox(r.Context(), tx, "trace", env)
	})
	if err != nil {
		return err
	}
	o, err := s.Store.GetOutcome(r.Context(), s.Store.Pool, t.ProjectID, t.TraceID)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, o)
	return nil
}

func nullJSON(raw json.RawMessage) any {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	return raw
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func (s *Server) flag(w http.ResponseWriter, r *http.Request) error {
	p, t, err := s.lookup(r, authn.PermReviewWrite)
	if err != nil {
		return err
	}
	var in struct {
		Kind   string `json:"kind"`
		Reason string `json:"reason"`
	}
	if err := httpx.DecodeJSON(w, r, &in, 16<<10); err != nil {
		return err
	}
	if in.Kind == "" {
		in.Kind = "manual"
	}
	if in.Kind != "incident" && in.Kind != "negative_feedback" && in.Kind != "manual" {
		return httpx.Invalid("INVALID_FLAG", "kind must be incident, negative_feedback or manual.", map[string]any{"field": "kind"})
	}
	in.Reason = strings.TrimSpace(in.Reason)
	if in.Reason == "" || utf8.RuneCountInString(in.Reason) > 2000 {
		return httpx.Invalid("INVALID_FLAG", "reason is required (max 2000 characters).", map[string]any{"field": "reason"})
	}
	flag := store.Flag{ID: ids.New(), Kind: in.Kind, Reason: in.Reason, FlaggedBy: p.Actor, CreatedAt: s.Now().UTC()}
	err = db.WithTx(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(), `INSERT INTO trace.trace_flag (id, project_id, trace_id, kind, reason, flagged_by, created_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7)`, flag.ID, t.ProjectID, t.TraceID, flag.Kind, flag.Reason, flag.FlaggedBy, flag.CreatedAt); err != nil {
			return err
		}
		if _, err := tx.Exec(r.Context(), `UPDATE trace.trace SET flagged = true WHERE project_id = $1 AND trace_id = $2`, t.ProjectID, t.TraceID); err != nil {
			return err
		}
		env, err := events.New("trace.flagged.v1", Producer, p.OrgID, t.ProjectID, logx.RequestID(r.Context()), "", map[string]any{
			"trace_id": t.TraceID, "reason": in.Reason, "kind": in.Kind, "flagged_by": p.Actor,
		})
		if err != nil {
			return err
		}
		return events.WriteOutbox(r.Context(), tx, "trace", env)
	})
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, flag)
	return nil
}

func (s *Server) stats(w http.ResponseWriter, r *http.Request) error {
	p, err := authn.Require(r, authn.PermRead)
	if err != nil {
		return err
	}
	q := r.URL.Query()
	projectID, err := projectFilter(r, p)
	if err != nil {
		return err
	}
	to := s.Now().UTC()
	from := to.Add(-7 * 24 * time.Hour)
	if t, err := parseTime(q.Get("from"), "from"); err != nil {
		return err
	} else if t != nil {
		from = *t
	}
	if t, err := parseTime(q.Get("to"), "to"); err != nil {
		return err
	} else if t != nil {
		to = *t
	}
	if !from.Before(to) {
		return httpx.Invalid("INVALID_FILTER", "from must be before to.", map[string]any{"field": "from"})
	}
	st, err := s.Store.Stats(r.Context(), scopeOf(p), projectID, from, to)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "stats": st})
	return nil
}

// facets returns filter options for the trace explorer.
func (s *Server) facets(w http.ResponseWriter, r *http.Request) error {
	p, err := authn.Require(r, authn.PermRead)
	if err != nil {
		return err
	}
	q := r.URL.Query()
	projectID, err := projectFilter(r, p)
	if err != nil {
		return err
	}
	from := s.Now().UTC().Add(-30 * 24 * time.Hour)
	if t, err := parseTime(q.Get("from"), "from"); err != nil {
		return err
	} else if t != nil {
		from = *t
	}
	fs, sampled, err := s.Store.Facets(r.Context(), scopeOf(p), projectID, from)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, map[string]any{"from": from, "facets": fs, "sampled": sampled})
	return nil
}

// emitAudit publishes an audit.recorded.v1 event for the central audit log.
func emitAudit(ctx context.Context, tx pgx.Tx, p authn.Principal, projectID, action, resourceType, resourceID, reason string) error {
	env, err := events.New("audit.recorded.v1", Producer, p.OrgID, projectID, logx.RequestID(ctx), "", map[string]any{
		"actor": p.Actor, "action": action, "resource_type": resourceType, "resource_id": resourceID,
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "request_id": nilIfEmpty(logx.RequestID(ctx)),
		"reason": nilIfEmpty(reason), "metadata": map[string]any{},
	})
	if err != nil {
		return fmt.Errorf("audit event: %w", err)
	}
	return events.WriteOutbox(ctx, tx, "trace", env)
}
