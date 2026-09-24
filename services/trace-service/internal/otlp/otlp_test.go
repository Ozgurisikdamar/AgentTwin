package otlp

import (
	"bytes"
	"compress/gzip"
	"encoding/hex"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	coltracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/protobuf/proto"
)

func init() { Now = func() time.Time { return time.Unix(1790000000, 0).Add(time.Hour) } }

const jsonReq = `{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"agent"}}]},
 "scopeSpans":[{"scope":{"name":"s","version":"1"},"spans":[
  {"traceId":"0af7651916cd43dd8448eb211c80319c","spanId":"b7ad6b7169203331","name":"agent.run","kind":1,
   "startTimeUnixNano":"1790000000000000000","endTimeUnixNano":"1790000002000000000",
   "attributes":[{"key":"n","value":{"intValue":"42"}},{"key":"f","value":{"doubleValue":1.5}},{"key":"b","value":{"boolValue":true}},
     {"key":"arr","value":{"arrayValue":{"values":[{"stringValue":"a"},{"intValue":2}]}}},
     {"key":"kv","value":{"kvlistValue":{"values":[{"key":"x","value":{"stringValue":"y"}}]}}}],
   "events":[{"timeUnixNano":"1790000001000000000","name":"exception","attributes":[{"key":"exception.type","value":{"stringValue":"TimeoutError"}}]}],
   "status":{"code":2,"message":"boom"}},
  {"traceId":"00000000000000000000000000000000","spanId":"b7ad6b7169203332","name":"bad","startTimeUnixNano":"1","endTimeUnixNano":"2"},
  {"traceId":"0af7651916cd43dd8448eb211c80319c","spanId":"b7ad6b7169203333","name":"backwards","startTimeUnixNano":"1790000002000000000","endTimeUnixNano":"1790000001000000000"}
 ]}]}]}`

func TestDecodeJSON(t *testing.T) {
	res, err := Decode([]byte(jsonReq), "application/json; charset=utf-8", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Spans) != 1 || res.Rejected != 2 || len(res.Errors) != 2 {
		t.Fatalf("spans=%d rejected=%d errors=%v", len(res.Spans), res.Rejected, res.Errors)
	}
	s := res.Spans[0]
	if s.TraceID != "0af7651916cd43dd8448eb211c80319c" || s.SpanID != "b7ad6b7169203331" || s.StatusCode != 2 || s.StatusMessage != "boom" {
		t.Fatalf("span: %+v", s)
	}
	want := map[string]any{"n": int64(42), "f": 1.5, "b": true, "arr": []any{"a", int64(2)}, "kv": map[string]any{"x": "y"}}
	if !reflect.DeepEqual(s.Attrs, want) {
		t.Fatalf("attrs = %#v", s.Attrs)
	}
	if s.Resource["service.name"] != "agent" || s.ScopeName != "s" || s.End.Sub(s.Start) != 2*time.Second {
		t.Fatalf("resource/scope/time: %+v", s)
	}
	if len(s.Events) != 1 || s.Events[0].Attrs["exception.type"] != "TimeoutError" {
		t.Fatalf("events: %+v", s.Events)
	}
}

func mustHex(s string) []byte { b, _ := hex.DecodeString(s); return b }

// TestProtobufMatchesJSON proves both encodings produce the same spans.
func TestProtobufMatchesJSON(t *testing.T) {
	req := &coltracepb.ExportTraceServiceRequest{ResourceSpans: []*tracepb.ResourceSpans{{
		Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{{Key: "service.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "agent"}}}}},
		ScopeSpans: []*tracepb.ScopeSpans{{
			Scope: &commonpb.InstrumentationScope{Name: "s", Version: "1"},
			Spans: []*tracepb.Span{{
				TraceId: mustHex("0af7651916cd43dd8448eb211c80319c"), SpanId: mustHex("b7ad6b7169203331"), Name: "agent.run",
				Kind: tracepb.Span_SPAN_KIND_INTERNAL, StartTimeUnixNano: 1790000000000000000, EndTimeUnixNano: 1790000002000000000,
				Attributes: []*commonpb.KeyValue{
					{Key: "n", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_IntValue{IntValue: 42}}},
					{Key: "f", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_DoubleValue{DoubleValue: 1.5}}},
					{Key: "b", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_BoolValue{BoolValue: true}}},
					{Key: "arr", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_ArrayValue{ArrayValue: &commonpb.ArrayValue{Values: []*commonpb.AnyValue{
						{Value: &commonpb.AnyValue_StringValue{StringValue: "a"}}, {Value: &commonpb.AnyValue_IntValue{IntValue: 2}}}}}}},
					{Key: "kv", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_KvlistValue{KvlistValue: &commonpb.KeyValueList{Values: []*commonpb.KeyValue{
						{Key: "x", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "y"}}}}}}}},
				},
				Events: []*tracepb.Span_Event{{TimeUnixNano: 1790000001000000000, Name: "exception", Attributes: []*commonpb.KeyValue{
					{Key: "exception.type", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "TimeoutError"}}}}}},
				Status: &tracepb.Status{Code: tracepb.Status_STATUS_CODE_ERROR, Message: "boom"},
			}},
		}},
	}}}
	b, err := proto.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	pres, err := Decode(b, "application/x-protobuf", DefaultLimits)
	if err != nil {
		t.Fatal(err)
	}
	jres, _ := Decode([]byte(jsonReq), "application/json", DefaultLimits)
	if len(pres.Spans) != 1 || !reflect.DeepEqual(pres.Spans[0], jres.Spans[0]) {
		t.Fatalf("protobuf and JSON differ:\nproto: %+v\njson:  %+v", pres.Spans, jres.Spans[0])
	}
}

func TestGzipAndBombLimit(t *testing.T) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	_, _ = gz.Write([]byte(jsonReq))
	_ = gz.Close()
	body, err := ReadBody(bytes.NewReader(buf.Bytes()), "gzip", 1<<20)
	if err != nil || string(body) != jsonReq {
		t.Fatalf("gzip round trip failed: %v", err)
	}
	// 10 MiB of zeros compresses to a few KiB; the decompressed limit must stop it.
	buf.Reset()
	gz = gzip.NewWriter(&buf)
	_, _ = gz.Write(bytes.Repeat([]byte{'0'}, 10<<20))
	_ = gz.Close()
	if buf.Len() > 64<<10 {
		t.Fatalf("test bomb unexpectedly large: %d", buf.Len())
	}
	if _, err := ReadBody(bytes.NewReader(buf.Bytes()), "gzip", 1<<20); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("bomb not stopped: %v", err)
	}
	if _, err := ReadBody(strings.NewReader("x"), "br", 10); err == nil {
		t.Fatal("unsupported encoding must be rejected")
	}
}

func TestLimits(t *testing.T) {
	lim := DefaultLimits
	lim.MaxSpans = 1
	two := strings.Replace(jsonReq, `"name":"bad"`, `"name":"ok"`, 1)
	if _, err := Decode([]byte(two), "application/json", lim); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("span limit: %v", err)
	}
	lim = DefaultLimits
	lim.MaxAttributes = 2
	res, err := Decode([]byte(jsonReq), "application/json", lim)
	if err != nil || len(res.Spans[0].Attrs) != 2 || !res.Spans[0].Truncated {
		t.Fatalf("attribute limit: %v %+v", err, res.Spans)
	}
	if _, err := Decode([]byte("{"), "application/json", DefaultLimits); err == nil {
		t.Fatal("malformed JSON must fail")
	}
	if _, err := Decode([]byte("{}"), "text/plain", DefaultLimits); err == nil {
		t.Fatal("unknown content type must fail")
	}
}

func FuzzDecodeJSON(f *testing.F) {
	f.Add([]byte(jsonReq))
	f.Add([]byte(`{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"zz"}]}]}]}`))
	f.Fuzz(func(t *testing.T, b []byte) {
		res, err := Decode(b, "application/json", DefaultLimits)
		if err != nil {
			return
		}
		for _, s := range res.Spans {
			if len(s.TraceID) != 32 || len(s.SpanID) != 16 || s.End.Before(s.Start) || s.Name == "" {
				t.Fatalf("invalid span accepted: %+v", s)
			}
		}
	})
}

func FuzzDecodeProto(f *testing.F) {
	f.Add([]byte{0x0a, 0x00})
	f.Fuzz(func(t *testing.T, b []byte) {
		res, err := Decode(b, "application/x-protobuf", DefaultLimits)
		if err != nil {
			return
		}
		for _, s := range res.Spans {
			if len(s.TraceID) != 32 || len(s.SpanID) != 16 || s.End.Before(s.Start) {
				t.Fatalf("invalid span accepted: %+v", s)
			}
		}
	})
}
