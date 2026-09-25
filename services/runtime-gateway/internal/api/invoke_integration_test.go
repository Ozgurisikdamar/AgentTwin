package api_test

import (
	"context"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	promtest "github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
)

// demo registers the demo tools and activates the refund policy.
func (h *harness) demo() (policyID string) {
	h.t.Helper()
	h.register("refund_payment", "WRITE_IRREVERSIBLE", map[string]any{"forward_headers": []string{"X-AgentTwin-Tenant"}})
	h.register("lookup_order", "READ", nil)
	policyID = h.createPolicy(refundPolicy)
	h.activate(policyID, 1)
	return policyID
}

// agentHeaders are what the demo agent sends with each call.
func agentHeaders(traceN int, key string) map[string]string {
	hs := map[string]string{
		"X-AgentTwin-Agent": "support-refund-agent", "X-AgentTwin-Agent-Version": "1.3.0",
		"Traceparent": "00-" + trace(traceN) + "-00f067aa0ba902b7-01", "X-AgentTwin-Tenant": "acme",
	}
	if key != "" {
		hs["Idempotency-Key"] = key
	}
	return hs
}

func with(hs map[string]string, k, v string) map[string]string {
	out := map[string]string{}
	for a, b := range hs {
		out[a] = b
	}
	out[k] = v
	return out
}

func expectRefused(t *testing.T, r reply, status int, code string) {
	t.Helper()
	if r.status != status || r.code() != code {
		t.Fatalf("want %d %s, got %d %s", status, code, r.status, r.body)
	}
}

// TestAnOverLimitRefundWaitsForAPersonAndRunsOnce is golden path steps
// 13-14 (spec §137): an over-limit refund requires approval, the person
// approves the exact action, it succeeds once, and a modified request cannot
// reuse the approval.
func TestAnOverLimitRefundWaitsForAPersonAndRunsOnce(t *testing.T) {
	h := newHarness(t)
	h.demo()
	agent := h.key("agent")
	refund := map[string]any{"order_id": "ORD-1003", "amount": 150, "reason": "damaged item"}
	hs := agentHeaders(1, "refund-ORD-1003")

	r := h.invoke(agent, "refund_payment", refund, hs)
	expectRefused(t, r, 403, "APPROVAL_REQUIRED")
	approvalID := r.details(t)["approval_id"].(string)
	if r.header.Get("X-AgentTwin-Decision") != "require_approval" || r.header.Get("X-AgentTwin-Policy") != "refund-limits" ||
		r.header.Get("X-AgentTwin-Policy-Version") != "1" || r.header.Get("X-AgentTwin-Policy-Rule") != "approval-above-100" ||
		r.details(t)["expires_at"] != t0.Add(20*time.Minute).Format(time.RFC3339) {
		t.Fatalf("decision %v %s", r.header, r.body)
	}
	if n := len(h.tools.received("refund_payment")); n != 0 {
		t.Fatalf("the tool was called %d times before approval", n)
	}
	// Asking again while pending gets the same request.
	if again := h.invoke(agent, "refund_payment", refund, hs); again.details(t)["approval_id"] != approvalID {
		t.Fatalf("asked again: %s", again.body)
	}

	// The agent polls; the token cannot be claimed before a person decides.
	if got := h.ok(200, agent, "GET", "/gateway/v1/approvals/"+approvalID, nil); got["status"] != "PENDING" ||
		got["summary"] != `refund_payment(amount=150, order_id="ORD-1003", reason="damaged item")` {
		t.Fatalf("poll %v", got)
	}
	h.refused(409, "APPROVAL_PENDING", agent, "POST", "/gateway/v1/approvals/"+approvalID+"/token", nil)

	// What the person sees before deciding (spec §41.9).
	detail := h.ok(200, h.reviewer(), "GET", h.q("/api/v1/approvals/"+approvalID), nil)
	if detail["tool"] != "refund_payment" || detail["risk"] != "WRITE_IRREVERSIBLE" || detail["rule"] != "approval-above-100" ||
		detail["reason"] != "Refunds above 100 need a person's approval." || detail["trace_id"] != trace(1) ||
		detail["agent_version"] != "1.3.0" || detail["arguments"].(map[string]any)["amount"] != 150.0 ||
		detail["decision"].(map[string]any)["outcome"] != "approval_required" {
		t.Fatalf("detail %v", detail)
	}
	h.refused(400, "INVALID_REASON", h.reviewer(), "POST", "/api/v1/approvals/"+approvalID+"/approve", map[string]any{"project_id": h.project})
	if r := h.do(h.engineer(), "POST", "/api/v1/approvals/"+approvalID+"/approve", map[string]any{"project_id": h.project, "reason": "ok"}, nil); r.status != 403 {
		t.Fatalf("an engineer cannot approve: %d", r.status)
	}
	approved := h.ok(200, h.reviewer(), "POST", "/api/v1/approvals/"+approvalID+"/approve",
		map[string]any{"project_id": h.project, "reason": "Customer sent photos of the damage."})
	if approved["status"] != "APPROVED" || approved["decided_by"] != "user:reviewer" {
		t.Fatalf("approved %v", approved)
	}
	h.refused(409, "APPROVAL_CLOSED", h.reviewer(), "POST", "/api/v1/approvals/"+approvalID+"/deny",
		map[string]any{"project_id": h.project, "reason": "changed my mind"})

	// Only the caller that asked may claim the token.
	h.refused(403, "NOT_REQUESTER", h.key("other"), "POST", "/gateway/v1/approvals/"+approvalID+"/token", nil)
	first := h.ok(201, agent, "POST", "/gateway/v1/approvals/"+approvalID+"/token", nil)
	tok := h.ok(201, agent, "POST", "/gateway/v1/approvals/"+approvalID+"/token", nil)["token"].(string)
	if tok == first["token"] || tok[:4] != "apt_" {
		t.Fatalf("a new claim mints a new token")
	}

	// The voided token no longer works; a modified request cannot reuse the
	// approval, and what it attempted is recorded.
	expectRefused(t, h.invoke(agent, "refund_payment", refund, with(hs, "X-AgentTwin-Approval-Token", first["token"].(string))),
		403, "APPROVAL_TOKEN_INVALID")
	modified := map[string]any{"order_id": "ORD-1003", "amount": 450, "reason": "damaged item"}
	r = h.invoke(agent, "refund_payment", modified, with(agentHeaders(1, "refund-ORD-1003-b"), "X-AgentTwin-Approval-Token", tok))
	expectRefused(t, r, 403, "APPROVAL_MISMATCH")
	if changes := fmt.Sprint(r.details(t)["changes"]); changes != "[map[after:450 before:150 path:amount]]" {
		t.Fatalf("changes %s", changes)
	}

	// The approved action runs once, with its key and forwarded headers.
	r = h.invoke(agent, "refund_payment", refund, with(hs, "X-AgentTwin-Approval-Token", tok))
	if r.status != 200 || r.json(t)["status"] != "refunded" || r.header.Get("X-AgentTwin-Decision") != "require_approval" ||
		r.header.Get("Set-Cookie") != "" {
		t.Fatalf("approved call: %d %v %s", r.status, r.header, r.body)
	}
	calls := h.tools.received("refund_payment")
	if len(calls) != 1 || calls[0].Body["amount"] != 150.0 || calls[0].Header.Get("Idempotency-Key") != "refund-ORD-1003" ||
		calls[0].Header.Get("X-Agenttwin-Tenant") != "acme" || calls[0].Header.Get("Traceparent") == "" ||
		calls[0].Header.Get("Authorization") != "" || calls[0].Header.Get("X-AgentTwin-Approval-Token") != "" {
		t.Fatalf("the tool received %+v", calls)
	}
	// A retry with the same key replays; a new call cannot use the approval again.
	replay := h.invoke(agent, "refund_payment", refund, with(hs, "X-AgentTwin-Approval-Token", tok))
	if replay.status != 200 || replay.header.Get("Idempotent-Replayed") != "true" || len(h.tools.received("refund_payment")) != 1 {
		t.Fatalf("replay %d %v", replay.status, replay.header)
	}
	expectRefused(t, h.invoke(agent, "refund_payment", refund, with(agentHeaders(1, "refund-again"), "X-AgentTwin-Approval-Token", tok)),
		403, "APPROVAL_USED")
	h.refused(403, "APPROVAL_USED", agent, "POST", "/gateway/v1/approvals/"+approvalID+"/token", nil)

	detail = h.ok(200, h.reviewer(), "GET", h.q("/api/v1/approvals/"+approvalID), nil)
	var results []string
	for _, a := range detail["attempts"].([]any) {
		results = append(results, a.(map[string]any)["result"].(string))
	}
	if detail["status"] != "USED" || !slices.Equal(results, []string{"mismatch", "executed", "used"}) ||
		detail["used_decision"].(map[string]any)["outcome"] != "executed" {
		t.Fatalf("after use %v %v", detail["status"], results)
	}
	if changes := detail["attempts"].([]any)[0].(map[string]any)["changes"].([]any); len(changes) != 1 {
		t.Fatalf("attempt changes %v", changes)
	}

	// The record: what was decided for each call.
	decisions := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?approval_id="+approvalID), nil)["items"].([]any)
	var outcomes []string
	for _, d := range decisions {
		outcomes = append(outcomes, d.(map[string]any)["outcome"].(string))
	}
	if !slices.Equal(outcomes, []string{"approval_refused", "executed", "approval_refused", "approval_required", "approval_required"}) {
		t.Fatalf("outcomes %v", outcomes)
	}
	violations := h.outbox("policy.violation_detected.v1")
	if len(violations) != 5 || violations[0]["decision"] != "require_approval" || violations[0]["trace_id"] != trace(1) {
		t.Fatalf("violations %d %v", len(violations), violations)
	}
	want := []string{"approval.approved", "approval.token_issued", "approval.token_issued", "approval.used"}
	if got := h.audits(); !slices.Equal(got[len(got)-4:], want) {
		t.Fatalf("audit %v", got)
	}

	// The metrics tell the same story, with bounded labels (spec §54).
	m := h.api.Metrics
	for _, c := range []struct {
		effect, outcome string
		want            float64
	}{
		{"require_approval", "approval_required", 2},
		{"require_approval", "approval_refused", 3}, // voided token, mismatch, used
		{"require_approval", "executed", 1},
		{"none", "replayed", 1},
	} {
		if got := promtest.ToFloat64(m.Decisions.WithLabelValues(c.effect, c.outcome)); got != c.want {
			t.Errorf("decisions{%s,%s} = %v, want %v", c.effect, c.outcome, got, c.want)
		}
	}
	for event, want := range map[string]float64{"requested": 1, "approved": 1, "token_claimed": 2, "used": 1,
		"token_refused": 3, "denied": 0} {
		if got := promtest.ToFloat64(m.Approvals.WithLabelValues(event)); got != want {
			t.Errorf("approvals{%s} = %v, want %v", event, got, want)
		}
	}
	if n := promtest.CollectAndCount(m.Upstream); n != 1 {
		t.Errorf("upstream series %d, want 1 (executed)", n)
	}
}

func TestPoliciesDecideEveryCall(t *testing.T) {
	h := newHarness(t)
	h.demo()
	agent := h.key("agent")

	// No policy guards the read-only tool: it is forwarded.
	r := h.invoke(agent, "lookup_order", map[string]any{"order_id": "ORD-1001"}, agentHeaders(2, ""))
	if r.status != 200 || r.json(t)["status"] != "delivered" || r.header.Get("X-AgentTwin-Decision") != "allow" ||
		r.header.Get("X-AgentTwin-Policy") != "" || r.header.Get("Content-Type") != "application/json" {
		t.Fatalf("lookup: %d %v %s", r.status, r.header, r.body)
	}

	// A small refund is allowed; a second one in the same conversation is not.
	r = h.invoke(agent, "refund_payment", map[string]any{"order_id": "ORD-1001", "amount": 40}, agentHeaders(3, "k1"))
	if r.status != 200 || r.header.Get("X-AgentTwin-Decision") != "allow" {
		t.Fatalf("small refund: %d %s", r.status, r.body)
	}
	r = h.invoke(agent, "refund_payment", map[string]any{"order_id": "ORD-1002", "amount": 30}, agentHeaders(3, "k2"))
	expectRefused(t, r, 403, "POLICY_DENIED")
	if r.header.Get("X-AgentTwin-Policy-Rule") != "one-refund-per-conversation" {
		t.Fatalf("rule %v", r.header)
	}
	// Above 500 is denied even with an approval token.
	expectRefused(t, h.invoke(agent, "refund_payment", map[string]any{"order_id": "ORD-1003", "amount": 900},
		with(agentHeaders(4, "k3"), "X-AgentTwin-Approval-Token", "apt_"+strings.Repeat("A", 43))), 403, "POLICY_DENIED")
	// A missing order is denied.
	expectRefused(t, h.invoke(agent, "refund_payment", map[string]any{"amount": 10}, agentHeaders(5, "k4")), 403, "POLICY_DENIED")

	// Refused before deciding.
	for name, c := range map[string]struct {
		tool    string
		body    any
		headers map[string]string
		status  int
		code    string
	}{
		"no key":        {"refund_payment", map[string]any{"order_id": "ORD-1", "amount": 1}, agentHeaders(6, ""), 400, "IDEMPOTENCY_KEY_REQUIRED"},
		"not an object": {"lookup_order", "[1,2]", nil, 400, "INVALID_ARGUMENTS"},
		"two values":    {"lookup_order", `{"a":1}{"b":2}`, nil, 400, "INVALID_ARGUMENTS"},
		"trace":         {"lookup_order", map[string]any{}, map[string]string{"Traceparent": "nope"}, 400, "INVALID_CONTEXT"},
		"keys disagree": {"lookup_order", map[string]any{"idempotency_key": "a"}, map[string]string{"Idempotency-Key": "b"}, 400, "INVALID_CONTEXT"},
		"unregistered":  {"send_email", map[string]any{}, nil, 404, "TOOL_NOT_REGISTERED"},
		"bad tool name": {"a%20b", map[string]any{}, nil, 400, "INVALID_PARAMETER"},
		"project":       {"lookup_order", map[string]any{}, map[string]string{"X-AgentTwin-Project": ids.New()}, 404, "NOT_FOUND"},
	} {
		if r := h.invoke(agent, c.tool, c.body, c.headers); r.status != c.status || r.code() != c.code {
			t.Errorf("%s: %d %s", name, r.status, r.body)
		}
	}
	if r := h.invoke(h.viewer(), "lookup_order", map[string]any{}, nil); r.status != 403 {
		t.Fatalf("a person without runtime:invoke: %d", r.status)
	}
	two := h.key("two")
	two.ProjectIDs = append(two.ProjectIDs, ids.New())
	expectRefused(t, h.invoke(two, "lookup_order", map[string]any{}, nil), 400, "PROJECT_REQUIRED")
	if r := h.invoke(two, "lookup_order", map[string]any{}, map[string]string{"X-AgentTwin-Project": h.project}); r.status != 200 {
		t.Fatalf("named project: %d %s", r.status, r.body)
	}

	var decided []string
	for _, v := range h.outbox("policy.violation_detected.v1") {
		decided = append(decided, v["decision"].(string)+":"+v["rule"].(string))
	}
	if !slices.Equal(decided, []string{"deny:one-refund-per-conversation", "deny:never-above-500", "deny:valid-order"}) {
		t.Fatalf("violations %v", decided)
	}
	// The record of each decision: redacted arguments, matched rules.
	denied := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?outcome=denied&tool=refund_payment"), nil)["items"].([]any)
	if len(denied) != 3 {
		t.Fatalf("denied %d", len(denied))
	}
	d := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions/"+denied[1].(map[string]any)["id"].(string)), nil)
	matched := d["decisions"].([]any)[0].(map[string]any)["matched"].([]any)
	if d["rule"] != "never-above-500" || len(matched) != 2 || d["subject"] != "apikey:agent" || d["version"] != nil {
		t.Fatalf("decision %v", d)
	}
	for filter, n := range map[string]int{"effect=allow": 3, "trace_id=" + trace(3): 2, "outcome=executed": 3} {
		if got := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?"+filter), nil)["items"].([]any); len(got) != n {
			t.Errorf("%s: %d", filter, len(got))
		}
	}
	page := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?limit=2"), nil)
	rest := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?limit=50&cursor="+page["next_cursor"].(string)), nil)["items"].([]any)
	if len(page["items"].([]any)) != 2 || len(rest) != 4 {
		t.Fatalf("pages %v %d", page["next_cursor"], len(rest))
	}
	for _, bad := range []string{"outcome=nope", "effect=nope", "trace_id=XYZ", "approval_id=1", "tool=a%20b", "cursor=%21"} {
		h.refused(400, map[bool]string{true: "INVALID_CURSOR", false: "INVALID_PARAMETER"}[strings.HasPrefix(bad, "cursor")],
			h.viewer(), "GET", h.q("/api/v1/policy-decisions?"+bad), nil)
	}
	h.refused(404, "NOT_FOUND", h.viewer(), "GET", h.q("/api/v1/policy-decisions/"+ids.New()), nil)
}

func TestIdempotencyKeys(t *testing.T) {
	h := newHarness(t)
	h.demo()
	agent := h.key("agent")
	refund := map[string]any{"order_id": "ORD-1001", "amount": 40}

	first := h.invoke(agent, "refund_payment", refund, agentHeaders(1, "same-key"))
	replay := h.invoke(agent, "refund_payment", refund, agentHeaders(1, "same-key"))
	if first.status != 200 || replay.status != 200 || string(replay.body) != string(first.body) ||
		replay.header.Get("Idempotent-Replayed") != "true" || replay.header.Get("X-AgentTwin-Decision") != "allow" ||
		len(h.tools.received("refund_payment")) != 1 {
		t.Fatalf("replay %d %s / %s", replay.status, first.body, replay.body)
	}
	if replays := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?outcome=replayed"), nil)["items"].([]any); len(replays) != 1 ||
		replays[0].(map[string]any)["effect"] != nil {
		t.Fatalf("the replay is recorded: %v", replays)
	}
	// The key in the arguments is the same key.
	args := map[string]any{"order_id": "ORD-1001", "amount": 40, "idempotency_key": "arg-key"}
	h.invoke(agent, "refund_payment", args, agentHeaders(7, ""))
	if again := h.invoke(agent, "refund_payment", args, agentHeaders(7, "")); again.header.Get("Idempotent-Replayed") != "true" {
		t.Fatalf("argument key %v", again.header)
	}
	expectRefused(t, h.invoke(agent, "refund_payment", map[string]any{"order_id": "ORD-1001", "amount": 41}, agentHeaders(1, "same-key")),
		409, "IDEMPOTENCY_KEY_REUSED")

	// A failing tool leaves the outcome unknown: the retry is forwarded again.
	h.register("fail", "WRITE_REVERSIBLE", map[string]any{"idempotency": "required"})
	r := h.invoke(agent, "fail", map[string]any{"x": 1}, agentHeaders(2, "fail-key"))
	if r.status != 500 || r.json(t)["error"] != "payments are down" {
		t.Fatalf("tool 500 is passed through: %d %s", r.status, r.body)
	}
	h.invoke(agent, "fail", map[string]any{"x": 1}, agentHeaders(2, "fail-key"))
	if n := len(h.tools.received("fail")); n != 2 {
		t.Fatalf("an unknown outcome is retried: %d calls", n)
	}
	failed := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?tool=fail"), nil)["items"].([]any)[0].(map[string]any)
	if failed["outcome"] != "failed" || failed["upstream_status"] != 500.0 || failed["completed_at"] == nil {
		t.Fatalf("failed decision %v", failed)
	}
	// A tool's own refusal completes the key.
	h.register("reject", "WRITE_REVERSIBLE", nil)
	for range 2 {
		// A JSON dialect is answered as JSON.
		if r := h.invoke(agent, "reject", map[string]any{"order_id": "X"}, agentHeaders(3, "reject-key")); r.status != 422 ||
			r.header.Get("Content-Type") != "application/json" {
			t.Fatalf("tool 422: %d %s", r.status, r.header.Get("Content-Type"))
		}
	}
	if n := len(h.tools.received("reject")); n != 1 {
		t.Fatalf("a completed refusal is replayed: %d calls", n)
	}

	// Concurrent calls with one key reach the tool once.
	h.register("lookup_order", "READ", map[string]any{"idempotency": "required"})
	var wg sync.WaitGroup
	statuses := make([]int, 12)
	for i := range statuses {
		wg.Add(1)
		go func() {
			defer wg.Done()
			statuses[i] = h.invoke(agent, "lookup_order", map[string]any{"order_id": "ORD-5"}, agentHeaders(9, "concurrent")).status
		}()
	}
	wg.Wait()
	for _, s := range statuses {
		if s != 200 && s != 409 {
			t.Fatalf("statuses %v", statuses)
		}
	}
	if n := len(h.tools.received("lookup_order")); n != 1 {
		t.Fatalf("concurrent calls with one key reached the tool %d times", n)
	}
	// One key, different actions at once: one runs, the others are refused.
	for i := range statuses {
		wg.Add(1)
		go func() {
			defer wg.Done()
			statuses[i] = h.invoke(agent, "lookup_order", map[string]any{"order_id": fmt.Sprintf("ORD-%d", 100+i)},
				agentHeaders(10+i, "shared")).status
		}()
	}
	wg.Wait()
	if n := len(h.tools.received("lookup_order")); n != 2 || slices.Index(statuses, 200) < 0 {
		t.Fatalf("one key, different actions: %d calls, %v", n, statuses)
	}
}

func TestConcurrentOverLimitCallsShareOneApproval(t *testing.T) {
	h := newHarness(t)
	h.demo()
	// Rounds of simultaneous calls for one action, each with its own key and
	// conversation: only the action is shared, and it gets one request.
	for round := range 4 {
		var wg sync.WaitGroup
		got := make([]string, 25)
		start := make(chan struct{})
		order := fmt.Sprintf("ORD-20%d", round)
		for i := range got {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				r := h.invoke(h.key("agent"), "refund_payment", map[string]any{"order_id": order, "amount": 150},
					agentHeaders(100*round+i, fmt.Sprintf("k-%d-%d", round, i)))
				if r.status == 403 {
					got[i] = r.details(t)["approval_id"].(string)
				}
			}()
		}
		close(start)
		wg.Wait()
		slices.Sort(got)
		if got[0] == "" || got[0] != got[len(got)-1] {
			t.Fatalf("round %d: approvals %v", round, got)
		}
	}
	if list := h.ok(200, h.viewer(), "GET", h.q("/api/v1/approvals"), nil)["items"].([]any); len(list) != 4 {
		t.Fatalf("%d approval requests", len(list))
	}
}

func TestApprovalsExpire(t *testing.T) {
	h := newHarness(t)
	h.demo()
	agent := h.key("agent")
	refund := map[string]any{"order_id": "ORD-1003", "amount": 150}
	ask := func(key string) string {
		r := h.invoke(agent, "refund_payment", refund, agentHeaders(1, key))
		expectRefused(t, r, 403, "APPROVAL_REQUIRED")
		return r.details(t)["approval_id"].(string)
	}
	approve := func(id string) {
		h.ok(200, h.reviewer(), "POST", "/api/v1/approvals/"+id+"/approve", map[string]any{"project_id": h.project, "reason": "ok"})
	}

	// A pending request past its expiry cannot be decided; a new one opens.
	first := ask("a")
	h.clock.advance(21 * time.Minute)
	// Read before anything closes it: the stored PENDING is shown as it is.
	if got := h.ok(200, h.viewer(), "GET", h.q("/api/v1/approvals/"+first), nil); got["status"] != "EXPIRED" {
		t.Fatalf("an expired request reads %v", got["status"])
	}
	if got := h.ok(200, h.viewer(), "GET", h.q("/api/v1/approvals"), nil)["items"].([]any); got[0].(map[string]any)["status"] != "EXPIRED" {
		t.Fatalf("listed as %v", got[0])
	}
	r := h.refused(409, "APPROVAL_CLOSED", h.reviewer(), "POST", "/api/v1/approvals/"+first+"/approve", map[string]any{"project_id": h.project, "reason": "late"})
	if r.details(t)["status"] != "EXPIRED" {
		t.Fatalf("closed %s", r.body)
	}
	var stored string
	if err := h.pool.QueryRow(context.Background(), `SELECT status FROM approval_request WHERE id = $1`, first).Scan(&stored); err != nil || stored != "EXPIRED" {
		t.Fatalf("the request is closed as EXPIRED: %q %v", stored, err)
	}
	second := ask("b")
	if second == first {
		t.Fatal("an expired request is not reused")
	}
	for status, n := range map[string]int{"EXPIRED": 1, "PENDING": 1, "": 2} {
		path := "/api/v1/approvals"
		if status != "" {
			path += "?status=" + status
		}
		if list := h.ok(200, h.viewer(), "GET", h.q(path), nil)["items"].([]any); len(list) != n {
			t.Errorf("status %q: %d", status, len(list))
		}
	}
	h.refused(400, "INVALID_PARAMETER", h.viewer(), "GET", h.q("/api/v1/approvals?status=OPEN"), nil)

	// A token lives at most 10 minutes.
	approve(second)
	tok := h.ok(201, agent, "POST", "/gateway/v1/approvals/"+second+"/token", nil)
	if tok["expires_at"] != t0.Add(31*time.Minute).Format(time.RFC3339) {
		t.Fatalf("token expiry %v", tok["expires_at"])
	}
	h.clock.advance(11 * time.Minute)
	expectRefused(t, h.invoke(agent, "refund_payment", refund, with(agentHeaders(1, "b"), "X-AgentTwin-Approval-Token", tok["token"].(string))),
		403, "APPROVAL_TOKEN_EXPIRED")
	// A fresh token works until the approval itself expires.
	fresh := h.ok(201, agent, "POST", "/gateway/v1/approvals/"+second+"/token", nil)
	if fresh["expires_at"] != t0.Add(41*time.Minute).Format(time.RFC3339) {
		t.Fatalf("capped by the approval: %v", fresh["expires_at"])
	}
	h.clock.advance(10 * time.Minute)
	h.refused(403, "APPROVAL_EXPIRED", agent, "POST", "/gateway/v1/approvals/"+second+"/token", nil)
	if err := h.pool.QueryRow(context.Background(), `SELECT status FROM approval_request WHERE id = $1`, second).Scan(&stored); err != nil || stored != "EXPIRED" {
		t.Fatalf("a claim after the expiry closes the request: %q %v", stored, err)
	}
	if got := h.ok(200, agent, "GET", "/gateway/v1/approvals/"+second, nil); got["status"] != "EXPIRED" {
		t.Fatalf("status %v", got["status"])
	}

	// A denied request says who denied it and why.
	third := ask("c")
	h.ok(200, h.reviewer(), "POST", "/api/v1/approvals/"+third+"/deny", map[string]any{"project_id": h.project, "reason": "no photos"})
	if a := h.audits(); a[len(a)-1] != "approval.denied" {
		t.Fatalf("audit %v", a)
	}
	r = h.refused(403, "APPROVAL_DENIED", agent, "POST", "/gateway/v1/approvals/"+third+"/token", nil)
	if r.details(t)["reason"] != "no photos" {
		t.Fatalf("denied %s", r.body)
	}
	h.refused(404, "NOT_FOUND", agent, "GET", "/gateway/v1/approvals/"+ids.New(), nil)
}

func TestTenancy(t *testing.T) {
	h := newHarness(t)
	h.demo()
	r := h.invoke(h.key("agent"), "refund_payment", map[string]any{"order_id": "ORD-1003", "amount": 150}, agentHeaders(1, "k"))
	approvalID := r.details(t)["approval_id"].(string)
	h.ok(200, h.reviewer(), "POST", "/api/v1/approvals/"+approvalID+"/approve", map[string]any{"project_id": h.project, "reason": "ok"})
	tok := h.ok(201, h.key("agent"), "POST", "/gateway/v1/approvals/"+approvalID+"/token", nil)["token"].(string)

	// Another project of the same organization: its key cannot use the token,
	// see the approval or its decisions.
	other := h.project
	h.project = ids.New()
	h.register("refund_payment", "WRITE_IRREVERSIBLE", nil)
	h.activate(h.createPolicy(refundPolicy), 1)
	elsewhere := h.key("agent")
	expectRefused(t, h.invoke(elsewhere, "refund_payment", map[string]any{"order_id": "ORD-1003", "amount": 150},
		with(agentHeaders(1, "k"), "X-AgentTwin-Approval-Token", tok)), 403, "APPROVAL_TOKEN_INVALID")
	h.refused(404, "NOT_FOUND", elsewhere, "GET", "/gateway/v1/approvals/"+approvalID, nil)
	h.refused(404, "NOT_FOUND", h.reviewer(), "GET", h.q("/api/v1/approvals/"+approvalID), nil)
	h.refused(404, "NOT_FOUND", h.reviewer(), "POST", "/api/v1/approvals/"+approvalID+"/deny", map[string]any{"project_id": h.project, "reason": "x"})
	h.project = other
	// Nothing was recorded on the approval.
	if attempts := h.ok(200, h.reviewer(), "GET", h.q("/api/v1/approvals/"+approvalID), nil)["attempts"].([]any); len(attempts) != 0 {
		t.Fatalf("attempts %v", attempts)
	}
	// A person of another organization.
	stranger := authn.Principal{OrgID: ids.New(), Actor: "user:s", Role: authn.RoleOwner, ProjectIDs: []string{h.project}}
	h.refused(404, "NOT_FOUND", stranger, "GET", h.q("/api/v1/approvals/"+approvalID), nil)
}

func TestToolFailuresAndLimits(t *testing.T) {
	h := newHarness(t)
	agent := h.key("agent")
	h.tools.slow = 2 * time.Second
	h.register("slow", "READ", map[string]any{"timeout_ms": 300})
	r := h.invoke(agent, "slow", map[string]any{}, nil)
	expectRefused(t, r, 504, "TOOL_TIMEOUT")
	d := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions/"+r.details(t)["decision_id"].(string)), nil)
	if d["outcome"] != "failed" || d["error_code"] != "TOOL_TIMEOUT" || d["upstream_status"] != nil {
		t.Fatalf("timeout decision %v", d)
	}

	// A policy's limits lower the timeout and the response cap.
	h.register("big", "READ", nil)
	h.register("huge", "READ", nil)
	expectRefused(t, h.invoke(agent, "huge", map[string]any{}, nil), 502, "TOOL_RESPONSE_TOO_LARGE")
	if r := h.invoke(agent, "big", map[string]any{}, nil); r.status != 200 {
		t.Fatalf("under the default cap: %d", r.status)
	}
	limits := `
apiVersion: agenttwin.dev/v1
kind: Policy
metadata: {name: small-answers}
spec:
  tool: big
  default: allow_with_limits
  limits: {maxResponseBytes: 1024}
  rules:
    - {name: never, when: "false", effect: deny}
  tests:
    - {name: limited, args: {}, expect: allow_with_limits}
`
	h.activate(h.createPolicy(limits), 1)
	r = h.invoke(agent, "big", map[string]any{}, nil)
	expectRefused(t, r, 502, "TOOL_RESPONSE_TOO_LARGE")
	if r.header.Get("X-AgentTwin-Decision") != "allow_with_limits" {
		t.Fatalf("decision %v", r.header)
	}

	// Answers that are not JSON or text are not passed as HTML; redirects
	// are not followed.
	h.register("html", "READ", nil)
	if r := h.invoke(agent, "html", map[string]any{}, nil); r.header.Get("Content-Type") != "application/octet-stream" {
		t.Fatalf("html answer served as %s", r.header.Get("Content-Type"))
	}
	h.ok(201, h.owner(), "PUT", "/api/v1/tool-endpoints/redirecting", map[string]any{"project_id": h.project,
		"url": h.tools.srv.URL + "/redirect", "risk": "READ"})
	expectRefused(t, h.invoke(agent, "redirecting", map[string]any{}, nil), 502, "EGRESS_BLOCKED")
	// A host that resolves somewhere it may not go is refused when dialled.
	h.ok(201, h.owner(), "PUT", "/api/v1/tool-endpoints/remote", map[string]any{"project_id": h.project,
		"url": "http://tools.example.com/tools/lookup_order", "risk": "READ"})
	if r := h.invoke(agent, "remote", map[string]any{}, nil); r.status != 502 {
		t.Fatalf("unresolvable host: %d %s", r.status, r.body)
	}

	// A rule that cannot be evaluated applies the fail mode.
	h.register("lookup_order", "READ", nil)
	failing := func(name, mode string) string {
		return fmt.Sprintf(`
apiVersion: agenttwin.dev/v1
kind: Policy
metadata: {name: %s}
spec:
  tool: lookup_order
  failMode: %s
  rules:
    - {name: needs-region, when: "args.region == 'eu'", effect: allow}
  tests:
    - {name: eu, args: {region: eu}, expect: allow}
`, name, mode)
	}
	id := h.createPolicy(failing("regions", "fail_closed"))
	h.activate(id, 1)
	r = h.invoke(agent, "lookup_order", map[string]any{"order_id": "ORD-1"}, nil)
	expectRefused(t, r, 403, "POLICY_DENIED")
	h.ok(201, h.owner(), "POST", "/api/v1/policies/"+id+"/versions", map[string]any{"project_id": h.project, "document": failing("regions", "fail_open")})
	h.activate(id, 2)
	if r := h.invoke(agent, "lookup_order", map[string]any{"order_id": "ORD-1"}, nil); r.status != 200 {
		t.Fatalf("fail_open on a READ tool: %d %s", r.status, r.body)
	}
	h.ok(201, h.owner(), "POST", "/api/v1/policies/"+id+"/versions", map[string]any{"project_id": h.project, "document": failing("regions", "require_approval")})
	h.activate(id, 3)
	expectRefused(t, h.invoke(agent, "lookup_order", map[string]any{"order_id": "ORD-1"}, nil), 403, "APPROVAL_REQUIRED")
}

func TestMCPTools(t *testing.T) {
	h := newHarness(t)
	agent := h.key("agent")
	for _, tool := range []string{"lookup_order", "broken", "confused"} {
		h.ok(201, h.owner(), "PUT", "/api/v1/tool-endpoints/"+tool, map[string]any{"project_id": h.project, "kind": "mcp",
			"url": h.tools.srv.URL + "/mcp", "risk": "READ"})
	}
	r := h.invoke(agent, "lookup_order", map[string]any{"order_id": "ORD-7"}, agentHeaders(1, ""))
	if r.status != http.StatusOK || !strings.Contains(string(r.body), "looked up ORD-7") {
		t.Fatalf("mcp: %d %s", r.status, r.body)
	}
	if calls := h.tools.received("mcp:lookup_order"); len(calls) != 1 || calls[0].Body["order_id"] != "ORD-7" {
		t.Fatalf("mcp calls %+v", calls)
	}
	expectRefused(t, h.invoke(agent, "broken", map[string]any{}, nil), 502, "TOOL_PROTOCOL_ERROR")
	expectRefused(t, h.invoke(agent, "confused", map[string]any{}, nil), 502, "TOOL_PROTOCOL_ERROR")
}

func TestWhatIsRecordedIsRedactedAndTheCallCompletesWithoutTheAgent(t *testing.T) {
	h := newHarness(t)
	agent := h.key("agent")
	h.register("lookup_order", "READ", nil)
	r := h.invoke(agent, "lookup_order", map[string]any{"order_id": "ORD-1", "api_key": "sk-live-0123456789abcdef",
		"note": "card 4111 1111 1111 1111"}, agentHeaders(1, ""))
	if r.status != 200 {
		t.Fatalf("lookup %d", r.status)
	}
	// The tool receives the arguments as sent; the record keeps them redacted.
	if got := h.tools.received("lookup_order")[0].Body["api_key"]; got != "sk-live-0123456789abcdef" {
		t.Fatalf("forwarded %v", got)
	}
	d := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions/"+r.header.Get("X-AgentTwin-Decision-Id")), nil)
	args := d["arguments"].(map[string]any)
	if args["api_key"] != "[REDACTED:field]" || strings.Contains(fmt.Sprint(args["note"]), "4111") ||
		strings.Contains(d["summary"].(string), "sk-live") || d["subject"] != "apikey:agent" || d["agent"] != "support-refund-agent" {
		t.Fatalf("recorded %v", d)
	}

	// The agent hangs up while the tool works: the call still completes and
	// is recorded.
	h.tools.slow = 400 * time.Millisecond
	h.register("slow", "READ", map[string]any{"timeout_ms": 5000})
	client := &http.Client{Timeout: 100 * time.Millisecond}
	req, _ := http.NewRequest("POST", h.srv.URL+"/gateway/v1/tools/slow", strings.NewReader("{}"))
	tok, _ := h.tokens.Mint(agent, "runtime-gateway", "rid-hangup")
	req.Header.Set("Authorization", "Bearer "+tok)
	req.Header.Set("Content-Type", "application/json")
	if _, err := client.Do(req); err == nil {
		t.Fatal("the client should have given up")
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		list := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions?tool=slow"), nil)["items"].([]any)
		if len(list) == 1 && list[0].(map[string]any)["outcome"] == "executed" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the call was not completed: %v", list)
		}
		time.Sleep(50 * time.Millisecond)
	}
	if n := len(h.tools.received("slow")); n != 1 {
		t.Fatalf("slow calls %d", n)
	}
}

func TestAnApprovalBindsTheAgent(t *testing.T) {
	h := newHarness(t)
	h.demo()
	agent := h.key("agent")
	refund := map[string]any{"order_id": "ORD-1003", "amount": 150}
	r := h.invoke(agent, "refund_payment", refund, agentHeaders(1, "k"))
	id := r.details(t)["approval_id"].(string)
	h.ok(200, h.reviewer(), "POST", "/api/v1/approvals/"+id+"/approve", map[string]any{"project_id": h.project, "reason": "ok"})
	tok := h.ok(201, agent, "POST", "/gateway/v1/approvals/"+id+"/token", nil)["token"].(string)
	// Another agent with the same arguments is another action.
	expectRefused(t, h.invoke(agent, "refund_payment", refund, with(with(agentHeaders(1, "k"), "X-AgentTwin-Agent", "other-agent"),
		"X-AgentTwin-Approval-Token", tok)), 403, "APPROVAL_MISMATCH")
	if r := h.invoke(agent, "refund_payment", refund, with(agentHeaders(1, "k"), "X-AgentTwin-Approval-Token", tok)); r.status != 200 {
		t.Fatalf("the approved agent: %d %s", r.status, r.body)
	}
}

func TestAPolicyLimitLowersTheTimeout(t *testing.T) {
	h := newHarness(t)
	h.tools.slow = 800 * time.Millisecond
	h.register("slow", "READ", map[string]any{"timeout_ms": 5000})
	h.activate(h.createPolicy(`
apiVersion: agenttwin.dev/v1
kind: Policy
metadata: {name: quick}
spec:
  tool: slow
  default: allow_with_limits
  limits: {timeoutMs: 200}
  rules:
    - {name: never, when: "false", effect: deny}
  tests:
    - {name: limited, args: {}, expect: allow_with_limits}
`), 1)
	r := h.invoke(h.key("agent"), "slow", map[string]any{}, nil)
	expectRefused(t, r, 504, "TOOL_TIMEOUT")
	if !strings.Contains(r.json(t)["error"].(map[string]any)["message"].(string), "200ms") {
		t.Fatalf("timeout message %s", r.body)
	}
	d := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policy-decisions/"+r.details(t)["decision_id"].(string)), nil)
	if d["limits"].(map[string]any)["timeoutMs"] != 200.0 || d["effect"] != "allow_with_limits" {
		t.Fatalf("limits %v", d["limits"])
	}
}

func TestRulesReadWhoCalls(t *testing.T) {
	h := newHarness(t)
	h.register("lookup_order", "READ", nil)
	h.activate(h.createPolicy(`
apiVersion: agenttwin.dev/v1
kind: Policy
metadata: {name: no-interns}
spec:
  tool: lookup_order
  rules:
    - {name: intern, when: "subject == 'apikey:intern' && environment == 'production'", effect: deny}
  tests:
    - {name: intern, args: {}, context: {subject: "apikey:intern", environment: production}, expect: deny}
    - {name: others, args: {}, context: {subject: "apikey:agent", environment: production}, expect: allow}
`), 1)
	expectRefused(t, h.invoke(h.key("intern"), "lookup_order", map[string]any{}, nil), 403, "POLICY_DENIED")
	if r := h.invoke(h.key("intern"), "lookup_order", map[string]any{}, map[string]string{"X-AgentTwin-Environment": "staging"}); r.status != 200 {
		t.Fatalf("staging: %d", r.status)
	}
	if r := h.invoke(h.key("agent"), "lookup_order", map[string]any{}, nil); r.status != 200 {
		t.Fatalf("agent: %d", r.status)
	}
}

// When the gateway cannot record its decision (here: the database refuses the
// insert), nothing is forwarded: the agent gets 503 and may retry (ADR-0033).
func TestADecisionThatCannotBeRecordedForwardsNothing(t *testing.T) {
	h := newHarness(t)
	h.demo()
	agent := h.key("agent")
	ctx := context.Background()
	if _, err := h.pool.Exec(ctx, `CREATE FUNCTION refuse_decision() RETURNS trigger LANGUAGE plpgsql AS
		$$ BEGIN RAISE EXCEPTION 'disk full'; END $$;
		CREATE TRIGGER refuse_decision BEFORE INSERT ON policy_decision FOR EACH ROW EXECUTE FUNCTION refuse_decision();`); err != nil {
		t.Fatal(err)
	}
	r := h.invoke(agent, "refund_payment", map[string]any{"order_id": "ORD-1001", "amount": 40}, agentHeaders(7, "k-down"))
	expectRefused(t, r, 503, "GATEWAY_UNAVAILABLE")
	if n := len(h.tools.received("refund_payment")); n != 0 {
		t.Fatalf("the tool was called %d times without a recorded decision", n)
	}
	if got := promtest.ToFloat64(h.api.Metrics.Decisions.WithLabelValues("none", "unavailable")); got != 1 {
		t.Fatalf("unavailable decisions %v", got)
	}

	// Once the database accepts it again, the same call (same key) is decided and runs once.
	if _, err := h.pool.Exec(ctx, `DROP TRIGGER refuse_decision ON policy_decision`); err != nil {
		t.Fatal(err)
	}
	r = h.invoke(agent, "refund_payment", map[string]any{"order_id": "ORD-1001", "amount": 40}, agentHeaders(7, "k-down"))
	if r.status != 200 || len(h.tools.received("refund_payment")) != 1 {
		t.Fatalf("after recovery: %d %s", r.status, r.body)
	}
}
