package gateway

import (
	"encoding/json"
	"errors"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"
)

func mustArgs(t *testing.T, body string) Args {
	t.Helper()
	a, err := ParseArgs([]byte(body))
	if err != nil {
		t.Fatalf("parse %s: %v", body, err)
	}
	return a
}

// ---------------------------------------------------------------- arguments and the action

func TestParseArgs(t *testing.T) {
	a := mustArgs(t, ` {"amount": 150.50, "order_id": "ORD-1003", "items": [1, 2e2]} `)
	if a.Values["amount"] != 150.5 || a.Values["order_id"] != "ORD-1003" || a.Values["items"].([]any)[1] != 200.0 {
		t.Fatalf("values %+v", a.Values)
	}
	if got := string(a.Canonical()); got != `{"amount":150.5,"items":[1,200],"order_id":"ORD-1003"}` {
		t.Fatalf("canonical %s", got)
	}
	empty := mustArgs(t, "  ")
	if len(empty.Values) != 0 || string(empty.Canonical()) != "{}" {
		t.Fatalf("empty body: %+v %s", empty.Values, empty.Canonical())
	}
	// A number beyond float64 cannot be evaluated by a policy: refused.
	for _, body := range []string{`[1]`, `"x"`, `null`, `{"a":1} {"b":2}`, `{"a":`, `{"a":1}x`, `{"n":1e400}`} {
		if _, err := ParseArgs([]byte(body)); !errors.Is(err, ErrArgs) {
			t.Fatalf("%s: %v", body, err)
		}
	}
	big := `{"a":"` + strings.Repeat("x", MaxArgsBytes) + `"}`
	if _, err := ParseArgs([]byte(big)); !errors.Is(err, ErrArgs) || !strings.Contains(err.Error(), "at most") {
		t.Fatalf("too big: %v", err)
	}
}

// A body naming a key twice means one value to the gateway; the tool gets
// the canonical bytes, so it cannot read the other.
func TestTheToolGetsWhatThePolicyDecided(t *testing.T) {
	a := mustArgs(t, `{"amount": 50, "amount": 5000}`)
	if a.Values["amount"] != 5000.0 || string(a.Canonical()) != `{"amount":5000}` {
		t.Fatalf("duplicate key: %+v %s", a.Values, a.Canonical())
	}
}

func TestTheActionHash(t *testing.T) {
	act := func(org, project, agent, tool, body string) Action {
		return Action{Organization: org, Project: project, Agent: agent, Tool: tool, Args: mustArgs(t, body)}
	}
	base := act("o", "p", "a", "refund_payment", `{"order_id":"ORD-1","amount":150}`)
	h := base.Hash()
	if len(h) != 64 {
		t.Fatalf("hash %q", h)
	}
	same := []Action{
		act("o", "p", "a", "refund_payment", `{"amount":150,"order_id":"ORD-1"}`),
		act("o", "p", "a", "refund_payment", "{\n \"amount\" : 150.0 , \"order_id\":\"ORD-1\"}"),
		act("o", "p", "a", "refund_payment", `{"amount":1.5e2,"order_id":"ORD-1"}`),
	}
	for _, s := range same {
		if s.Hash() != h {
			t.Fatalf("the same action hashes differently: %s", s.Args.Canonical())
		}
	}
	different := []Action{
		act("o", "p", "a", "refund_payment", `{"amount":150.01,"order_id":"ORD-1"}`),
		act("o", "p", "a", "refund_payment", `{"amount":150,"order_id":"ORD-2"}`),
		act("o", "p", "a", "refund_payment", `{"amount":150,"order_id":"ORD-1","note":null}`),
		act("o", "p", "a", "send_email", `{"amount":150,"order_id":"ORD-1"}`),
		act("o", "p", "b", "refund_payment", `{"amount":150,"order_id":"ORD-1"}`),
		act("o", "q", "a", "refund_payment", `{"amount":150,"order_id":"ORD-1"}`),
		act("x", "p", "a", "refund_payment", `{"amount":150,"order_id":"ORD-1"}`),
	}
	for i, d := range different {
		if d.Hash() == h {
			t.Fatalf("different action %d hashes the same", i)
		}
	}
	// Exact numbers: two amounts float64 cannot tell apart still differ.
	if act("o", "p", "a", "t", `{"n":9007199254740993}`).Hash() == act("o", "p", "a", "t", `{"n":9007199254740992}`).Hash() {
		t.Fatal("the hash must keep numbers exact")
	}
}

func TestDescribe(t *testing.T) {
	got := Describe("refund_payment", map[string]any{"order_id": "ORD-1003", "amount": 150.0, "note": "<b> & c"})
	if got != `refund_payment(amount=150, note="<b> & c", order_id="ORD-1003")` {
		t.Fatalf("describe %q", got)
	}
	if got := Describe("t", nil); got != "t()" {
		t.Fatalf("no args %q", got)
	}
	long := Describe("t", map[string]any{"s": strings.Repeat("é", 400)})
	if r := []rune(long); len(r) != MaxSummary || !strings.HasSuffix(long, "…") || !strings.HasPrefix(long, `t(s="éé`) {
		t.Fatalf("truncated %d %q", len(r), long[:20])
	}
	if got := Describe("t", map[string]any{"f": func() {}}); got != `t(f="?")` {
		t.Fatalf("unencodable %q", got)
	}
	if got := Describe("t", map[string]any{"a": 1}); got != "t(a=1)" {
		t.Fatalf("exact length %q", got)
	}
}

// ---------------------------------------------------------------- context

func header(kv ...string) http.Header {
	h := http.Header{}
	for i := 0; i < len(kv); i += 2 {
		h.Set(kv[i], kv[i+1])
	}
	return h
}

const traceID = "4bf92f3577b34da6a3ce929d0e0e4736"

func TestReadContext(t *testing.T) {
	c, ps := ReadContext(header(
		HeaderProject, "01a0d9c7-d08b-757c-b868-a6ff009548dd",
		HeaderAgent, "support-refund-agent", HeaderAgentVersion, "1.3.0",
		HeaderEnvironment, "staging", HeaderTraceparent, "00-"+strings.ToUpper(traceID)+"-00f067aa0ba902b7-01",
		HeaderIdempotencyKey, "refund-ORD-1003-150.00",
		HeaderApprovalToken, "apt_"+strings.Repeat("a", 43),
	), mustArgs(t, `{"idempotency_key":"refund-ORD-1003-150.00"}`))
	want := Context{Project: "01a0d9c7-d08b-757c-b868-a6ff009548dd", Agent: "support-refund-agent", AgentVersion: "1.3.0",
		Environment: "staging", TraceID: traceID, IdempotencyKey: "refund-ORD-1003-150.00", ApprovalToken: "apt_" + strings.Repeat("a", 43)}
	if len(ps) != 0 || c != want {
		t.Fatalf("context %+v problems %v", c, ps)
	}
	c, ps = ReadContext(http.Header{}, mustArgs(t, `{"idempotency_key":"k-1"}`))
	if len(ps) != 0 || c.Environment != DefaultEnvironment || c.IdempotencyKey != "k-1" || c.TraceID != "" {
		t.Fatalf("defaults %+v %v", c, ps)
	}
	c, _ = ReadContext(header(HeaderTraceID, traceID, HeaderTraceparent, "00-"+traceID+"-00f067aa0ba902b7-01"), Args{})
	if c.TraceID != traceID {
		t.Fatalf("agreeing trace headers: %+v", c)
	}
	c, _ = ReadContext(header(HeaderTraceID, strings.ToUpper(traceID)), Args{})
	if c.TraceID != traceID {
		t.Fatalf("explicit trace id: %+v", c)
	}
	c, _ = ReadContext(header(HeaderIdempotencyKey, "k"), mustArgs(t, `{"idempotency_key":null}`))
	if c.IdempotencyKey != "k" {
		t.Fatalf("a null argument leaves the header's key: %+v", c)
	}
}

func TestReadContextRefusesWhatItCannotUse(t *testing.T) {
	cases := map[string]struct {
		h     http.Header
		args  string
		field string
	}{
		"project":        {header(HeaderProject, "support"), `{}`, HeaderProject},
		"agent":          {header(HeaderAgent, "has space"), `{}`, HeaderAgent},
		"version":        {header(HeaderAgentVersion, strings.Repeat("v", 201)), `{}`, HeaderAgentVersion},
		"environment":    {header(HeaderEnvironment, "Prod"), `{}`, HeaderEnvironment},
		"token":          {header(HeaderApprovalToken, "apt_short"), `{}`, HeaderApprovalToken},
		"traceparent":    {header(HeaderTraceparent, "garbage"), `{}`, HeaderTraceparent},
		"zero parent":    {header(HeaderTraceparent, "00-"+strings.Repeat("0", 32)+"-00f067aa0ba902b7-01"), `{}`, HeaderTraceparent},
		"trace id":       {header(HeaderTraceID, "xyz"), `{}`, HeaderTraceID},
		"zero trace":     {header(HeaderTraceID, strings.Repeat("0", 32)), `{}`, HeaderTraceID},
		"two traces":     {header(HeaderTraceID, traceID, HeaderTraceparent, "00-"+strings.Repeat("a", 32)+"-00f067aa0ba902b7-01"), `{}`, HeaderTraceID},
		"key not string": {http.Header{}, `{"idempotency_key": 7}`, ArgIdempotencyKey},
		"bad header key": {header(HeaderIdempotencyKey, "has space"), `{}`, HeaderIdempotencyKey},
		"bad arg key":    {http.Header{}, `{"idempotency_key": "tab\there"}`, ArgIdempotencyKey},
		"long key":       {header(HeaderIdempotencyKey, strings.Repeat("k", 201)), `{}`, HeaderIdempotencyKey},
		"keys differ":    {header(HeaderIdempotencyKey, "a"), `{"idempotency_key": "b"}`, HeaderIdempotencyKey},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			_, ps := ReadContext(tc.h, mustArgs(t, tc.args))
			if len(ps) != 1 || ps[0].Field != tc.field || ps[0].Message == "" {
				t.Fatalf("problems %v, want one for %s", ps, tc.field)
			}
		})
	}
}

// ---------------------------------------------------------------- approvals

var now = time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)

func approved(t *testing.T) (Approval, string) {
	t.Helper()
	token, hash, err := NewToken()
	if err != nil {
		t.Fatal(err)
	}
	return Approval{
		ID: "ap", Organization: "o", Project: "p", Tool: "refund_payment", ActionHash: "h",
		Status: StatusApproved, ExpiresAt: now.Add(15 * time.Minute), RequestedBy: "apikey:k",
		TokenHash: hash, TokenExpiresAt: now.Add(TokenTTL),
	}, token
}

func TestTokens(t *testing.T) {
	a, _, err := NewToken()
	if err != nil {
		t.Fatal(err)
	}
	b, hash, _ := NewToken()
	if a == b || !TokenPattern.MatchString(a) || !strings.HasPrefix(a, TokenPrefix) || HashToken(b) != hash || len(hash) != 64 || hash == b {
		t.Fatalf("tokens %q %q %q", a, b, hash)
	}
	if got := TokenExpiry(now, now.Add(time.Hour)); !got.Equal(now.Add(TokenTTL)) {
		t.Fatalf("capped by the TTL: %v", got)
	}
	if got := TokenExpiry(now, now.Add(time.Minute)); !got.Equal(now.Add(time.Minute)) {
		t.Fatalf("capped by the approval: %v", got)
	}
}

func TestEffectiveStatus(t *testing.T) {
	for status, want := range map[string]string{StatusPending: StatusExpired, StatusApproved: StatusExpired,
		StatusDenied: StatusDenied, StatusUsed: StatusUsed, StatusExpired: StatusExpired} {
		a := Approval{Status: status, ExpiresAt: now}
		if got := a.Effective(now); got != want {
			t.Fatalf("%s at expiry reads %s", status, got)
		}
		if got := a.Effective(now.Add(-time.Nanosecond)); got != status {
			t.Fatalf("%s before expiry reads %s", status, got)
		}
	}
}

func TestDecideAndClaim(t *testing.T) {
	pending := Approval{Status: StatusPending, ExpiresAt: now.Add(time.Minute), RequestedBy: "apikey:k"}
	if err := CanDecide(pending, now); err != nil {
		t.Fatal(err)
	}
	if err := CanDecide(pending, now.Add(time.Minute)); !errors.Is(err, ErrApprovalClosed) || !strings.Contains(err.Error(), "EXPIRED") {
		t.Fatalf("expired: %v", err)
	}
	for _, s := range []string{StatusApproved, StatusDenied, StatusUsed} {
		if err := CanDecide(Approval{Status: s, ExpiresAt: now.Add(time.Hour)}, now); !errors.Is(err, ErrApprovalClosed) {
			t.Fatalf("%s: %v", s, err)
		}
	}
	a, _ := approved(t)
	if err := CanClaim(a, "apikey:k", now); err != nil {
		t.Fatal(err)
	}
	cases := map[string]struct {
		a      Approval
		caller string
		err    error
	}{
		"another caller": {a, "apikey:other", ErrNotRequester},
		"pending":        {pending, "apikey:k", ErrApprovalNotApproved},
		"denied":         {Approval{Status: StatusDenied, ExpiresAt: now.Add(time.Hour), RequestedBy: "apikey:k"}, "apikey:k", ErrApprovalNotApproved},
		"used":           {Approval{Status: StatusUsed, ExpiresAt: now.Add(time.Hour), RequestedBy: "apikey:k"}, "apikey:k", ErrApprovalUsed},
		"expired":        {Approval{Status: StatusApproved, ExpiresAt: now, RequestedBy: "apikey:k"}, "apikey:k", ErrApprovalExpired},
	}
	for name, tc := range cases {
		if err := CanClaim(tc.a, tc.caller, now); !errors.Is(err, tc.err) {
			t.Fatalf("%s: %v", name, err)
		}
	}
}

func TestCheckToken(t *testing.T) {
	a, token := approved(t)
	check := func(a Approval, token, org, project, tool, hash string, at time.Time) error {
		return CheckToken(a, token, org, project, tool, hash, at)
	}
	if err := check(a, token, "o", "p", "refund_payment", "h", now); err != nil {
		t.Fatalf("the exact action: %v", err)
	}
	other, _, _ := NewToken()
	used := a
	used.Status = StatusUsed
	denied := a
	denied.Status = StatusDenied
	noToken := a
	noToken.TokenHash = ""
	cases := map[string]struct {
		err  error
		call func() error
	}{
		"another token":            {ErrTokenInvalid, func() error { return check(a, other, "o", "p", "refund_payment", "h", now) }},
		"no token minted":          {ErrTokenInvalid, func() error { return check(noToken, token, "o", "p", "refund_payment", "h", now) }},
		"another organization":     {ErrTokenInvalid, func() error { return check(a, token, "x", "p", "refund_payment", "h", now) }},
		"another project":          {ErrTokenInvalid, func() error { return check(a, token, "o", "x", "refund_payment", "h", now) }},
		"used":                     {ErrApprovalUsed, func() error { return check(used, token, "o", "p", "refund_payment", "h", now) }},
		"denied":                   {ErrApprovalNotApproved, func() error { return check(denied, token, "o", "p", "refund_payment", "h", now) }},
		"approval expired":         {ErrApprovalExpired, func() error { return check(a, token, "o", "p", "refund_payment", "h", a.ExpiresAt) }},
		"token expired":            {ErrApprovalExpired, func() error { return check(a, token, "o", "p", "refund_payment", "h", a.TokenExpiresAt) }},
		"modified arguments":       {ErrApprovalMismatch, func() error { return check(a, token, "o", "p", "refund_payment", "h2", now) }},
		"another tool":             {ErrApprovalMismatch, func() error { return check(a, token, "o", "p", "send_email", "h", now) }},
		"used, modified arguments": {ErrApprovalUsed, func() error { return check(used, token, "o", "p", "refund_payment", "h2", now) }},
	}
	for name, tc := range cases {
		if err := tc.call(); !errors.Is(err, tc.err) {
			t.Fatalf("%s: %v, want %v", name, err, tc.err)
		}
	}
	if err := check(a, token, "o", "p", "refund_payment", "h", a.TokenExpiresAt.Add(-time.Nanosecond)); err != nil {
		t.Fatalf("just before the token expires: %v", err)
	}
}

// ---------------------------------------------------------------- idempotency

func TestIdempotency(t *testing.T) {
	rec := func(hash, state string, age time.Duration) *IdemRecord {
		return &IdemRecord{ActionHash: hash, State: state, UpdatedAt: now.Add(-age)}
	}
	cases := []struct {
		name string
		rec  *IdemRecord
		want IdemVerdict
	}{
		{"new key", nil, IdemProceed},
		{"completed", rec("h", IdemCompleted, time.Hour), IdemReplay},
		{"another action", rec("other", IdemCompleted, 0), IdemReused},
		{"another action in flight", rec("other", IdemInProgress, 0), IdemReused},
		{"in flight", rec("h", IdemInProgress, StaleAfter-time.Nanosecond), IdemBusy},
		{"abandoned", rec("h", IdemInProgress, StaleAfter), IdemProceed},
		{"unknown", rec("h", IdemUnknown, 0), IdemProceed},
	}
	for _, tc := range cases {
		if got := CheckIdempotency(tc.rec, "h", now); got != tc.want {
			t.Fatalf("%s: %s, want %s", tc.name, got, tc.want)
		}
	}
	states := []struct {
		status int
		err    error
		want   string
	}{
		{200, nil, IdemCompleted}, {201, nil, IdemCompleted}, {409, nil, IdemCompleted}, {422, nil, IdemCompleted},
		{400, nil, IdemCompleted}, {499, nil, IdemCompleted},
		{408, nil, IdemUnknown}, {429, nil, IdemUnknown}, {500, nil, IdemUnknown}, {503, nil, IdemUnknown},
		{0, errors.New("timeout"), IdemUnknown}, {200, errors.New("cut off"), IdemUnknown},
	}
	for _, s := range states {
		if got := StateAfter(s.status, s.err); got != s.want {
			t.Fatalf("%d %v: %s", s.status, s.err, got)
		}
	}
}

// ---------------------------------------------------------------- redaction and diff

func TestRedactArgs(t *testing.T) {
	in := map[string]any{
		"order_id": "ORD-1003", "amount": 150.0, "idempotency_key": "refund-ORD-1003",
		"note":     "customer jane@example.com asked; card 4111 1111 1111 1111",
		"password": "hunter2", "API-Key": "x", "authToken": nil,
		"nested":  map[string]any{"client_secret": "s", "ok": true, "list": []any{"bob@example.com", 1.0}},
		"secrets": []any{"a", "b"},
		"long":    strings.Repeat("z", MaxShownString+10),
	}
	got := RedactArgs(in).(map[string]any)
	want := map[string]any{
		"order_id": "ORD-1003", "amount": 150.0, "idempotency_key": "refund-ORD-1003",
		"note":     "customer [REDACTED:email] asked; card [REDACTED:card]",
		"password": "[REDACTED:field]", "API-Key": "[REDACTED:field]", "authToken": nil,
		"nested":  map[string]any{"client_secret": "[REDACTED:field]", "ok": true, "list": []any{"[REDACTED:email]", 1.0}},
		"secrets": "[REDACTED:field]",
	}
	long := got["long"].(string)
	delete(got, "long")
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("redacted\n got %v\nwant %v", got, want)
	}
	if len(long) > MaxShownString+20 || !strings.HasPrefix(long, "zzz") || len(long) >= MaxShownString+10 {
		t.Fatalf("truncated to %d", len(long))
	}
	if in["password"] != "hunter2" || in["nested"].(map[string]any)["client_secret"] != "s" {
		t.Fatal("the input must not change")
	}
	if RedactArgs(nil) != nil || RedactArgs(3.0) != 3.0 {
		t.Fatal("scalars")
	}
}

func TestDiffArgs(t *testing.T) {
	approvedArgs := mustArgs(t, `{"order_id":"ORD-1003","amount":150,"items":[{"sku":"a"},{"sku":"b"}],"meta":{"x":1},"gone":true,"null":null}`)
	attempted := mustArgs(t, `{"order_id":"ORD-1003","amount":1500,"items":[{"sku":"a"},{"sku":"c"},{"sku":"d"}],"meta":{"x":1.0,"y":2},"null":null,"extra":null}`)
	got := DiffArgs(approvedArgs.exact, attempted.Values)
	want := []Change{
		{Path: "amount", Before: 150.0, After: 1500.0},
		{Path: "extra", Added: true},
		{Path: "gone", Before: true, Removed: true},
		{Path: "items[1].sku", Before: "b", After: "c"},
		{Path: "items[2]", After: map[string]any{"sku": "d"}, Added: true},
		{Path: "meta.y", After: 2.0, Added: true},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("diff\n got %+v\nwant %+v", got, want)
	}
	if d := DiffArgs(attempted.Values, approvedArgs.Values); len(d) != 6 || d[4].Path != "items[2]" || !d[4].Removed {
		t.Fatalf("reverse diff %+v", d)
	}
	if d := DiffArgs(approvedArgs.Values, approvedArgs.exact); len(d) != 0 {
		t.Fatalf("the same arguments: %+v", d)
	}
	if d := DiffArgs(map[string]any{"a": []any{1}}, map[string]any{"a": "x"}); len(d) != 1 || d[0].Path != "a" {
		t.Fatalf("type change: %+v", d)
	}
	if d := DiffArgs(map[string]any{"n": 1}, map[string]any{"n": int64(1)}); len(d) != 0 {
		t.Fatalf("ints: %+v", d)
	}
	if d := DiffArgs(map[string]any{"n": 3}, map[string]any{"n": 4.0}); len(d) != 1 || d[0].Before != 3.0 {
		t.Fatalf("int vs float: %+v", d)
	}
	if d := DiffArgs(map[string]any{"n": json.Number("1e400")}, map[string]any{"n": "1e400"}); len(d) != 0 {
		t.Fatalf("a number beyond float64 compares as its text: %+v", d)
	}
}
