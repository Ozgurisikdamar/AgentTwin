// Package otlp decodes OTLP/HTTP trace export requests (JSON and protobuf,
// optionally gzip-compressed) into a flat, validated span list. It knows the
// wire format only; semantic interpretation lives in package semconv.
package otlp

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"
	"time"

	coltracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	"google.golang.org/protobuf/proto"
)

// Limits bound what a single export request may carry.
type Limits struct {
	MaxDecompressedBytes int64
	MaxSpans             int
	MaxAttributes        int // per span/resource/event; extra attributes are dropped
	MaxEvents            int // per span
	MaxDepth             int // nesting of array/kvlist values
}

// DefaultLimits are conservative defaults for the ingest endpoint.
var DefaultLimits = Limits{MaxDecompressedBytes: 32 << 20, MaxSpans: 20000, MaxAttributes: 256, MaxEvents: 128, MaxDepth: 8}

// ErrTooLarge is returned when the (decompressed) payload exceeds the limit.
var ErrTooLarge = errors.New("otlp payload too large")

// ErrUnsupported is returned for a Content-Type or Content-Encoding the
// endpoint does not accept.
var ErrUnsupported = errors.New("unsupported")

func mediaType(contentType string) string {
	return strings.ToLower(strings.TrimSpace(strings.SplitN(contentType, ";", 2)[0]))
}

// Protobuf reports whether contentType selects the binary protobuf encoding;
// the response to such a request is protobuf too.
func Protobuf(contentType string) bool {
	switch mediaType(contentType) {
	case "application/x-protobuf", "application/protobuf":
		return true
	}
	return false
}

// CheckContentType reports whether contentType is an accepted encoding.
func CheckContentType(contentType string) error {
	if mediaType(contentType) == "application/json" || Protobuf(contentType) {
		return nil
	}
	return fmt.Errorf("%w Content-Type %q (use application/json or application/x-protobuf)", ErrUnsupported, contentType)
}

// Event is a span event.
type Event struct {
	Name  string         `json:"name"`
	Time  time.Time      `json:"time"`
	Attrs map[string]any `json:"attributes,omitempty"`
}

// Span is one decoded span with its resource and scope.
type Span struct {
	Resource      map[string]any
	ScopeName     string
	ScopeVersion  string
	TraceID       string // 32 lowercase hex
	SpanID        string // 16 lowercase hex
	ParentSpanID  string // empty for roots
	Name          string
	Kind          int // OTel SpanKind enum
	Start         time.Time
	End           time.Time
	Attrs         map[string]any
	Events        []Event
	StatusCode    int // 0 unset, 1 ok, 2 error
	StatusMessage string
	Truncated     bool // attributes/events were dropped by limits
}

// Result is a decoded request.
type Result struct {
	Spans    []Span
	Rejected int
	Errors   []string // first few rejection reasons
}

func (r *Result) reject(reason string) {
	r.Rejected++
	if len(r.Errors) < 5 {
		r.Errors = append(r.Errors, reason)
	}
}

// ReadBody reads a possibly gzip-compressed body with a hard limit on the
// decompressed size (defends against compression bombs).
func ReadBody(body io.Reader, contentEncoding string, limit int64) ([]byte, error) {
	r := body
	switch strings.ToLower(strings.TrimSpace(contentEncoding)) {
	case "", "identity":
	case "gzip":
		gz, err := gzip.NewReader(body)
		if err != nil {
			return nil, fmt.Errorf("invalid gzip body: %w", err)
		}
		defer func() { _ = gz.Close() }()
		r = gz
	default:
		return nil, fmt.Errorf("%w Content-Encoding %q (use gzip or none)", ErrUnsupported, contentEncoding)
	}
	b, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, ErrTooLarge
	}
	return b, nil
}

// Decode parses an OTLP ExportTraceServiceRequest. contentType selects the
// encoding ("application/json" or "application/x-protobuf").
func Decode(b []byte, contentType string, lim Limits) (Result, error) {
	if err := CheckContentType(contentType); err != nil {
		return Result{}, err
	}
	if Protobuf(contentType) {
		return decodeProto(b, lim)
	}
	return decodeJSON(b, lim)
}

// ---- JSON (OTLP/HTTP JSON encoding: hex ids, int64 as strings, enums as ints)

type jsonRequest struct {
	ResourceSpans []struct {
		Resource struct {
			Attributes []jsonKV `json:"attributes"`
		} `json:"resource"`
		ScopeSpans []struct {
			Scope struct {
				Name    string `json:"name"`
				Version string `json:"version"`
			} `json:"scope"`
			Spans []jsonSpan `json:"spans"`
		} `json:"scopeSpans"`
	} `json:"resourceSpans"`
}

type jsonKV struct {
	Key   string          `json:"key"`
	Value json.RawMessage `json:"value"`
}

type jsonSpan struct {
	TraceID           string          `json:"traceId"`
	SpanID            string          `json:"spanId"`
	ParentSpanID      string          `json:"parentSpanId"`
	Name              string          `json:"name"`
	Kind              json.RawMessage `json:"kind"`
	StartTimeUnixNano json.RawMessage `json:"startTimeUnixNano"`
	EndTimeUnixNano   json.RawMessage `json:"endTimeUnixNano"`
	Attributes        []jsonKV        `json:"attributes"`
	Events            []struct {
		TimeUnixNano json.RawMessage `json:"timeUnixNano"`
		Name         string          `json:"name"`
		Attributes   []jsonKV        `json:"attributes"`
	} `json:"events"`
	Status struct {
		Code    json.RawMessage `json:"code"`
		Message string          `json:"message"`
	} `json:"status"`
}

type jsonAnyValue struct {
	StringValue *string         `json:"stringValue"`
	BoolValue   *bool           `json:"boolValue"`
	IntValue    json.RawMessage `json:"intValue"`
	DoubleValue json.RawMessage `json:"doubleValue"`
	BytesValue  *string         `json:"bytesValue"`
	ArrayValue  *struct {
		Values []json.RawMessage `json:"values"`
	} `json:"arrayValue"`
	KvlistValue *struct {
		Values []jsonKV `json:"values"`
	} `json:"kvlistValue"`
}

func decodeJSON(b []byte, lim Limits) (Result, error) {
	var req jsonRequest
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	if err := dec.Decode(&req); err != nil {
		return Result{}, fmt.Errorf("invalid OTLP JSON: %w", err)
	}
	var res Result
	total := 0
	for _, rs := range req.ResourceSpans {
		resource, _ := jsonAttrs(rs.Resource.Attributes, lim)
		for _, ss := range rs.ScopeSpans {
			for _, js := range ss.Spans {
				total++
				if total > lim.MaxSpans {
					return Result{}, fmt.Errorf("%w: more than %d spans in one request", ErrTooLarge, lim.MaxSpans)
				}
				sp, err := fromJSONSpan(js, lim)
				if err != nil {
					res.reject(err.Error())
					continue
				}
				sp.Resource = resource
				sp.ScopeName, sp.ScopeVersion = ss.Scope.Name, ss.Scope.Version
				res.Spans = append(res.Spans, sp)
			}
		}
	}
	return res, nil
}

func fromJSONSpan(js jsonSpan, lim Limits) (Span, error) {
	sp := Span{Name: js.Name, StatusMessage: js.Status.Message}
	var err error
	if sp.TraceID, err = normID(js.TraceID, 16); err != nil {
		return sp, fmt.Errorf("traceId: %w", err)
	}
	if sp.SpanID, err = normID(js.SpanID, 8); err != nil {
		return sp, fmt.Errorf("spanId: %w", err)
	}
	if js.ParentSpanID != "" {
		if sp.ParentSpanID, err = normID(js.ParentSpanID, 8); err != nil {
			return sp, fmt.Errorf("parentSpanId: %w", err)
		}
	}
	kind, err := jsonEnum(js.Kind, spanKindNames)
	if err != nil {
		return sp, fmt.Errorf("kind: %w", err)
	}
	sp.Kind = int(kind)
	start, err := jsonUint(js.StartTimeUnixNano)
	if err != nil {
		return sp, fmt.Errorf("startTimeUnixNano: %w", err)
	}
	end, err := jsonUint(js.EndTimeUnixNano)
	if err != nil {
		return sp, fmt.Errorf("endTimeUnixNano: %w", err)
	}
	if err := setTimes(&sp, start, end); err != nil {
		return sp, err
	}
	code, err := jsonEnum(js.Status.Code, statusCodeNames)
	if err != nil {
		return sp, fmt.Errorf("status.code: %w", err)
	}
	sp.StatusCode = int(code)
	var trunc bool
	sp.Attrs, trunc = jsonAttrs(js.Attributes, lim)
	sp.Truncated = trunc
	for i, ev := range js.Events {
		if i >= lim.MaxEvents {
			sp.Truncated = true
			break
		}
		t, err := jsonUint(ev.TimeUnixNano)
		if err != nil {
			continue
		}
		attrs, tr := jsonAttrs(ev.Attributes, lim)
		sp.Truncated = sp.Truncated || tr
		sp.Events = append(sp.Events, Event{Name: ev.Name, Time: unixNano(t), Attrs: attrs})
	}
	if sp.Name == "" {
		return sp, errors.New("span name is empty")
	}
	return sp, nil
}

func jsonAttrs(kvs []jsonKV, lim Limits) (map[string]any, bool) {
	out := make(map[string]any, min(len(kvs), lim.MaxAttributes))
	truncated := false
	for _, kv := range kvs {
		if kv.Key == "" {
			continue
		}
		if len(out) >= lim.MaxAttributes {
			truncated = true
			break
		}
		v, ok := jsonValue(kv.Value, lim.MaxDepth)
		if ok {
			out[kv.Key] = v
		}
	}
	return out, truncated
}

func jsonValue(raw json.RawMessage, depth int) (any, bool) {
	if len(raw) == 0 || depth <= 0 {
		return nil, false
	}
	var v jsonAnyValue
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, false
	}
	switch {
	case v.StringValue != nil:
		return *v.StringValue, true
	case v.BoolValue != nil:
		return *v.BoolValue, true
	case len(v.IntValue) > 0:
		n, err := jsonInt(v.IntValue)
		return n, err == nil
	case len(v.DoubleValue) > 0:
		f, err := jsonFloat(v.DoubleValue)
		return f, err == nil
	case v.BytesValue != nil:
		return *v.BytesValue, true // already base64 on the wire
	case v.ArrayValue != nil:
		out := make([]any, 0, len(v.ArrayValue.Values))
		for _, e := range v.ArrayValue.Values {
			if x, ok := jsonValue(e, depth-1); ok {
				out = append(out, x)
			}
		}
		return out, true
	case v.KvlistValue != nil:
		out := map[string]any{}
		for _, kv := range v.KvlistValue.Values {
			if x, ok := jsonValue(kv.Value, depth-1); ok {
				out[kv.Key] = x
			}
		}
		return out, true
	}
	return nil, false
}

// jsonInt accepts a JSON number or a decimal string (int64 encoding).
func jsonInt(raw json.RawMessage) (int64, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return 0, nil
	}
	s := strings.Trim(string(raw), `"`)
	return strconv.ParseInt(s, 10, 64)
}

var spanKindNames = map[string]int64{
	"SPAN_KIND_UNSPECIFIED": 0, "SPAN_KIND_INTERNAL": 1, "SPAN_KIND_SERVER": 2,
	"SPAN_KIND_CLIENT": 3, "SPAN_KIND_PRODUCER": 4, "SPAN_KIND_CONSUMER": 5,
}

var statusCodeNames = map[string]int64{"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}

// jsonEnum accepts the integer form mandated by OTLP/JSON and, leniently, the
// enum name some encoders emit.
func jsonEnum(raw json.RawMessage, names map[string]int64) (int64, error) {
	if n, err := jsonInt(raw); err == nil {
		return n, nil
	}
	if v, ok := names[strings.Trim(string(raw), `"`)]; ok {
		return v, nil
	}
	return 0, fmt.Errorf("unknown enum value %s", raw)
}

func jsonUint(raw json.RawMessage) (uint64, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return 0, errors.New("missing")
	}
	s := strings.Trim(string(raw), `"`)
	return strconv.ParseUint(s, 10, 64)
}

func jsonFloat(raw json.RawMessage) (float64, error) {
	s := strings.Trim(string(raw), `"`)
	switch s {
	case "NaN", "Infinity", "-Infinity":
		return 0, errors.New("non-finite double")
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
		return 0, errors.New("invalid double")
	}
	return f, nil
}

// ---- protobuf

func decodeProto(b []byte, lim Limits) (Result, error) {
	var req coltracepb.ExportTraceServiceRequest
	if err := proto.Unmarshal(b, &req); err != nil {
		return Result{}, fmt.Errorf("invalid OTLP protobuf: %w", err)
	}
	var res Result
	total := 0
	for _, rs := range req.GetResourceSpans() {
		resource, _ := protoAttrs(rs.GetResource().GetAttributes(), lim)
		for _, ss := range rs.GetScopeSpans() {
			for _, ps := range ss.GetSpans() {
				total++
				if total > lim.MaxSpans {
					return Result{}, fmt.Errorf("%w: more than %d spans in one request", ErrTooLarge, lim.MaxSpans)
				}
				sp := Span{Resource: resource, ScopeName: ss.GetScope().GetName(), ScopeVersion: ss.GetScope().GetVersion(),
					Name: ps.GetName(), Kind: int(ps.GetKind()), StatusCode: int(ps.GetStatus().GetCode()), StatusMessage: ps.GetStatus().GetMessage()}
				var err error
				if sp.TraceID, err = bytesID(ps.GetTraceId(), 16); err != nil {
					res.reject("traceId: " + err.Error())
					continue
				}
				if sp.SpanID, err = bytesID(ps.GetSpanId(), 8); err != nil {
					res.reject("spanId: " + err.Error())
					continue
				}
				if len(ps.GetParentSpanId()) > 0 {
					if sp.ParentSpanID, err = bytesID(ps.GetParentSpanId(), 8); err != nil {
						res.reject("parentSpanId: " + err.Error())
						continue
					}
				}
				if err := setTimes(&sp, ps.GetStartTimeUnixNano(), ps.GetEndTimeUnixNano()); err != nil {
					res.reject(err.Error())
					continue
				}
				if sp.Name == "" {
					res.reject("span name is empty")
					continue
				}
				sp.Attrs, sp.Truncated = protoAttrs(ps.GetAttributes(), lim)
				for i, ev := range ps.GetEvents() {
					if i >= lim.MaxEvents {
						sp.Truncated = true
						break
					}
					attrs, tr := protoAttrs(ev.GetAttributes(), lim)
					sp.Truncated = sp.Truncated || tr
					sp.Events = append(sp.Events, Event{Name: ev.GetName(), Time: unixNano(ev.GetTimeUnixNano()), Attrs: attrs})
				}
				res.Spans = append(res.Spans, sp)
			}
		}
	}
	return res, nil
}

func protoAttrs(kvs []*commonpb.KeyValue, lim Limits) (map[string]any, bool) {
	out := make(map[string]any, min(len(kvs), lim.MaxAttributes))
	truncated := false
	for _, kv := range kvs {
		if kv.GetKey() == "" {
			continue
		}
		if len(out) >= lim.MaxAttributes {
			truncated = true
			break
		}
		if v, ok := protoValue(kv.GetValue(), lim.MaxDepth); ok {
			out[kv.GetKey()] = v
		}
	}
	return out, truncated
}

func protoValue(v *commonpb.AnyValue, depth int) (any, bool) {
	if v == nil || depth <= 0 {
		return nil, false
	}
	switch x := v.GetValue().(type) {
	case *commonpb.AnyValue_StringValue:
		return x.StringValue, true
	case *commonpb.AnyValue_BoolValue:
		return x.BoolValue, true
	case *commonpb.AnyValue_IntValue:
		return x.IntValue, true
	case *commonpb.AnyValue_DoubleValue:
		if math.IsNaN(x.DoubleValue) || math.IsInf(x.DoubleValue, 0) {
			return nil, false
		}
		return x.DoubleValue, true
	case *commonpb.AnyValue_BytesValue:
		return base64.StdEncoding.EncodeToString(x.BytesValue), true
	case *commonpb.AnyValue_ArrayValue:
		out := make([]any, 0, len(x.ArrayValue.GetValues()))
		for _, e := range x.ArrayValue.GetValues() {
			if y, ok := protoValue(e, depth-1); ok {
				out = append(out, y)
			}
		}
		return out, true
	case *commonpb.AnyValue_KvlistValue:
		out := map[string]any{}
		for _, kv := range x.KvlistValue.GetValues() {
			if y, ok := protoValue(kv.GetValue(), depth-1); ok {
				out[kv.GetKey()] = y
			}
		}
		return out, true
	}
	return nil, false
}

// ---- shared helpers

// maxSkew bounds timestamps: spans far in the future or before 2000 are
// rejected rather than corrupting ordering and retention.
const maxFutureSkew = 24 * time.Hour

var minTimestamp = time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)

// Now is the clock used for timestamp sanity checks (tests override it).
var Now = time.Now

func setTimes(sp *Span, start, end uint64) error {
	if start == 0 || end == 0 {
		return errors.New("span start and end timestamps are required")
	}
	if start > math.MaxInt64 || end > math.MaxInt64 {
		return errors.New("span timestamp overflows")
	}
	sp.Start, sp.End = unixNano(start), unixNano(end)
	if sp.End.Before(sp.Start) {
		return errors.New("span ends before it starts")
	}
	if sp.Start.Before(minTimestamp) || sp.Start.After(Now().Add(maxFutureSkew)) {
		return fmt.Errorf("span start %s is outside the accepted time range", sp.Start.Format(time.RFC3339))
	}
	return nil
}

func unixNano(n uint64) time.Time {
	if n > math.MaxInt64 {
		n = math.MaxInt64
	}
	return time.Unix(0, int64(n)).UTC() //nolint:gosec // clamped to MaxInt64 above
}

// normID validates a hex id of n bytes (lowercased); all-zero ids are invalid.
func normID(s string, n int) (string, error) {
	s = strings.ToLower(strings.TrimSpace(s))
	if len(s) != 2*n {
		return "", fmt.Errorf("must be %d hex characters", 2*n)
	}
	b, err := hex.DecodeString(s)
	if err != nil {
		return "", errors.New("must be hex")
	}
	if isZero(b) {
		return "", errors.New("must not be all zeros")
	}
	return s, nil
}

func bytesID(b []byte, n int) (string, error) {
	if len(b) != n {
		return "", fmt.Errorf("must be %d bytes", n)
	}
	if isZero(b) {
		return "", errors.New("must not be all zeros")
	}
	return hex.EncodeToString(b), nil
}

func isZero(b []byte) bool {
	for _, c := range b {
		if c != 0 {
			return false
		}
	}
	return true
}
