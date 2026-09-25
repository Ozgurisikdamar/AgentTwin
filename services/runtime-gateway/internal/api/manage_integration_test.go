package api_test

import (
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
)

func TestToolEndpoints(t *testing.T) {
	h := newHarness(t)
	url := h.tools.url("refund_payment")
	put := func(p authn.Principal, body map[string]any) reply {
		b := map[string]any{"project_id": h.project, "url": url, "risk": "WRITE_IRREVERSIBLE"}
		for k, v := range body {
			if v == nil {
				delete(b, k)
			} else {
				b[k] = v
			}
		}
		return h.do(p, "PUT", "/api/v1/tool-endpoints/refund_payment", b, nil)
	}

	r := put(h.owner(), map[string]any{"forward_headers": []string{"x-agenttwin-tenant", "X-AgentTwin-Tenant"}})
	if r.status != 201 {
		t.Fatalf("register: %d %s", r.status, r.body)
	}
	e := r.json(t)
	// An irreversible tool requires a key; headers are canonical and unique.
	if e["kind"] != "http" || e["idempotency"] != "required" || e["timeout_ms"] != 10000.0 ||
		fmt.Sprint(e["forward_headers"]) != "[X-Agenttwin-Tenant]" || e["updated_by"] != "user:owner" {
		t.Fatalf("defaults: %v", e)
	}
	r = put(h.owner(), map[string]any{"timeout_ms": 2000, "idempotency": "optional"})
	if r.status != 200 || r.json(t)["timeout_ms"] != 2000.0 || r.json(t)["idempotency"] != "optional" {
		t.Fatalf("replace: %d %s", r.status, r.body)
	}
	if got := h.ok(200, h.viewer(), "GET", h.q("/api/v1/tool-endpoints/refund_payment"), nil); got["timeout_ms"] != 2000.0 {
		t.Fatalf("read: %v", got)
	}
	h.register("lookup_order", "READ", nil)
	list := h.ok(200, h.viewer(), "GET", h.q("/api/v1/tool-endpoints"), nil)["items"].([]any)
	if len(list) != 2 || list[0].(map[string]any)["tool"] != "lookup_order" {
		t.Fatalf("list: %v", list)
	}

	for name, c := range map[string]struct {
		body  map[string]any
		code  string
		field string
	}{
		"kind":              {map[string]any{"kind": "grpc"}, "INVALID_ENDPOINT", "kind"},
		"risk":              {map[string]any{"risk": "SPICY"}, "INVALID_ENDPOINT", "risk"},
		"timeout":           {map[string]any{"timeout_ms": 50}, "INVALID_ENDPOINT", "timeout_ms"},
		"idempotency":       {map[string]any{"idempotency": "sometimes"}, "INVALID_ENDPOINT", "idempotency"},
		"credential header": {map[string]any{"forward_headers": []string{"Authorization"}}, "INVALID_ENDPOINT", "forward_headers[0]"},
		"approval token":    {map[string]any{"forward_headers": []string{"X-AgentTwin-Approval-Token"}}, "INVALID_ENDPOINT", "forward_headers[0]"},
		"header name":       {map[string]any{"forward_headers": []string{"X Bad"}}, "INVALID_ENDPOINT", "forward_headers[0]"},
		"too many headers":  {map[string]any{"forward_headers": strings.Split("a,b,c,d,e,f,g,h,i,j,k", ",")}, "INVALID_ENDPOINT", "forward_headers"},
		"no url":            {map[string]any{"url": nil}, "INVALID_ENDPOINT", "url"},
		"relative url":      {map[string]any{"url": "/tools/x"}, "INVALID_ENDPOINT", "url"},
		"fragment":          {map[string]any{"url": url + "#x"}, "INVALID_ENDPOINT", "url"},
		"host":              {map[string]any{"url": "https://evil.example.org/refund"}, "EGRESS_NOT_ALLOWED", "url"},
		"metadata":          {map[string]any{"url": "http://169.254.169.254/latest"}, "EGRESS_NOT_ALLOWED", "url"},
		"credentials":       {map[string]any{"url": "https://user:pw@tools.example.com/x"}, "EGRESS_NOT_ALLOWED", "url"},
		"scheme":            {map[string]any{"url": "ftp://tools.example.com/x"}, "EGRESS_NOT_ALLOWED", "url"},
		"project":           {map[string]any{"project_id": nil}, "INVALID_PARAMETER", "project_id"},
	} {
		r := put(h.owner(), c.body)
		if r.status != 400 || r.code() != c.code || r.details(t)["field"] != c.field {
			t.Errorf("%s: %d %s %s", name, r.status, r.code(), r.body)
		}
	}
	if r := put(h.engineer(), nil); r.status != 403 {
		t.Fatalf("an engineer cannot register tools: %d", r.status)
	}
	other := authn.Principal{OrgID: ids.New(), Actor: "user:x", Role: authn.RoleOwner, ProjectIDs: []string{ids.New()}}
	if r := put(other, nil); r.status != 404 {
		t.Fatalf("another organization's project: %d", r.status)
	}
	h.refused(400, "INVALID_PARAMETER", h.owner(), "GET", h.q("/api/v1/tool-endpoints/bad%20name"), nil)
	h.refused(404, "NOT_FOUND", h.viewer(), "GET", h.q("/api/v1/tool-endpoints/send_email"), nil)

	h.ok(204, h.owner(), "DELETE", h.q("/api/v1/tool-endpoints/refund_payment"), nil)
	h.refused(404, "NOT_FOUND", h.owner(), "DELETE", h.q("/api/v1/tool-endpoints/refund_payment"), nil)
	want := []string{"tool_endpoint.registered", "tool_endpoint.updated", "tool_endpoint.registered", "tool_endpoint.removed"}
	if got := h.audits(); !slices.Equal(got, want) {
		t.Fatalf("audit %v", got)
	}
	// The audit keeps the host, not the URL.
	if meta := h.outbox("audit.recorded.v1")[0]["metadata"].(map[string]any); meta["host"] != strings.TrimPrefix(h.tools.srv.URL, "http://") || meta["url"] != nil {
		t.Fatalf("audit metadata %v", meta)
	}
}

func TestPolicyLifecycle(t *testing.T) {
	h := newHarness(t)
	h.register("refund_payment", "WRITE_IRREVERSIBLE", nil)
	doc := map[string]any{"project_id": h.project, "document": refundPolicy}

	if r := h.do(h.engineer(), "POST", "/api/v1/policies", doc, nil); r.status != 403 {
		t.Fatalf("an engineer cannot write policies: %d", r.status)
	}
	created := h.ok(201, h.owner(), "POST", "/api/v1/policies", doc)
	pol := created["policy"].(map[string]any)
	id := pol["id"].(string)
	v1 := created["version"].(map[string]any)
	if pol["name"] != "refund-limits" || pol["tool"] != "refund_payment" || pol["active"] != nil ||
		v1["version"] != 1.0 || v1["document"] != refundPolicy || v1["active"] != false {
		t.Fatalf("created %v", created)
	}
	r := h.refused(409, "POLICY_EXISTS", h.owner(), "POST", "/api/v1/policies", doc)
	if r.details(t)["field"] != "metadata.name" {
		t.Fatalf("conflict %s", r.body)
	}

	// Every problem is reported with its field.
	bad := strings.Replace(refundPolicy, "effect: require_approval", "effect: maybe", 1)
	r = h.refused(400, "INVALID_POLICY", h.owner(), "POST", "/api/v1/policies", map[string]any{"project_id": h.project, "document": bad})
	if problems := r.details(t)["problems"].([]any); len(problems) != 1 ||
		problems[0].(map[string]any)["field"] != "spec.rules[3].effect" {
		t.Fatalf("problems %v", problems)
	}
	r = h.refused(400, "INVALID_POLICY", h.owner(), "POST", "/api/v1/policies",
		map[string]any{"project_id": h.project, "document": strings.Replace(refundPolicy, "trace.calls >= 1", "tool >= 1", 1)})
	if f := r.details(t)["problems"].([]any)[0].(map[string]any)["field"]; f != "spec.rules[1].when" {
		t.Fatalf("type error field %v", f)
	}
	h.refused(400, "INVALID_JSON", h.owner(), "POST", "/api/v1/policies",
		map[string]any{"project_id": h.project, "document": refundPolicy, "extra": true})

	detail := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policies/"+id), nil)
	if vs := detail["versions"].([]any); len(vs) != 1 || vs[0].(map[string]any)["active"] != false {
		t.Fatalf("detail %v", detail)
	}
	version := h.ok(200, h.viewer(), "GET", h.q("/api/v1/policies/"+id+"/versions/1"), nil)
	var paths []string
	for _, th := range version["thresholds"].([]any) {
		m := th.(map[string]any)
		paths = append(paths, fmt.Sprintf("%s %s %v", m["path"], m["op"], m["value"]))
	}
	if !slices.Equal(paths, []string{"trace.calls >= 1", "args.amount > 500", "args.amount > 100"}) ||
		!slices.Contains(version["variables"].([]any), any("trace")) {
		t.Fatalf("thresholds %v", paths)
	}
	h.refused(404, "NOT_FOUND", h.viewer(), "GET", h.q("/api/v1/policies/"+id+"/versions/9"), nil)
	h.refused(400, "INVALID_PARAMETER", h.viewer(), "GET", h.q("/api/v1/policies/"+id+"/versions/0"), nil)
	h.refused(404, "NOT_FOUND", h.viewer(), "GET", h.q("/api/v1/policies/"+ids.New()), nil)

	// A new version; the same document again is not another version.
	v2doc := strings.Replace(refundPolicy, "args.amount > 500", "args.amount > 400", 1)
	v2doc = strings.Replace(v2doc, "amount: 900}", "amount: 450}", 1)
	v2doc = strings.Replace(v2doc, "description: Refunds the support agent may issue on its own.", "description: Refunds up to 400.", 1)
	added := h.ok(201, h.owner(), "POST", "/api/v1/policies/"+id+"/versions", map[string]any{"project_id": h.project, "document": v2doc})
	if added["version"].(map[string]any)["version"] != 2.0 || added["created"] != true || added["policy"].(map[string]any)["latest_version"] != 2.0 ||
		added["policy"].(map[string]any)["description"] != "Refunds up to 400." {
		t.Fatalf("added %v", added)
	}
	same := h.ok(200, h.owner(), "POST", "/api/v1/policies/"+id+"/versions", map[string]any{"project_id": h.project, "document": v2doc + "\n# a comment\n"})
	if same["created"] != false || same["version"].(map[string]any)["version"] != 2.0 {
		t.Fatalf("unchanged %v", same)
	}
	for field, change := range map[string][2]string{
		"metadata.name": {"name: refund-limits", "name: refunds"},
		"spec.tool":     {"tool: refund_payment", "tool: send_email"},
	} {
		r := h.refused(400, "INVALID_POLICY", h.owner(), "POST", "/api/v1/policies/"+id+"/versions",
			map[string]any{"project_id": h.project, "document": strings.Replace(v2doc, change[0], change[1], 1)})
		if r.details(t)["problems"].([]any)[0].(map[string]any)["field"] != field {
			t.Fatalf("%s: %s", field, r.body)
		}
	}

	// A version whose tests fail cannot be activated.
	failing := strings.Replace(refundPolicy, "expect: allow", "expect: deny", 1)
	h.ok(201, h.owner(), "POST", "/api/v1/policies/"+id+"/versions", map[string]any{"project_id": h.project, "document": failing})
	r = h.refused(422, "POLICY_NOT_ACTIVATABLE", h.owner(), "POST", "/api/v1/policies/"+id+"/activate", map[string]any{"project_id": h.project, "version": 3})
	if !strings.Contains(fmt.Sprint(r.details(t)["problems"]), "small refund") {
		t.Fatalf("problems %s", r.body)
	}
	if r := h.do(h.engineer(), "POST", "/api/v1/policies/"+id+"/activate", map[string]any{"project_id": h.project, "version": 1}, nil); r.status != 403 {
		t.Fatalf("an engineer cannot activate: %d", r.status)
	}
	h.refused(404, "NOT_FOUND", h.owner(), "POST", "/api/v1/policies/"+id+"/activate", map[string]any{"project_id": h.project, "version": 7})
	active := h.ok(200, h.owner(), "POST", "/api/v1/policies/"+id+"/activate", map[string]any{"project_id": h.project, "version": 1, "reason": "launch"})
	if active["active"].(map[string]any)["version"] != 1.0 || active["activated_by"] != "user:owner" {
		t.Fatalf("active %v", active)
	}
	h.activate(id, 1) // the active version again changes nothing
	h.activate(id, 2)
	events := h.outbox("policy.activated.v1")
	if len(events) != 2 || events[0]["version"] != 1.0 || events[1]["version"] != 2.0 || events[1]["target_tool"] != "refund_payment" ||
		events[1]["activated_by"] != "user:owner" {
		t.Fatalf("activation events %v", events)
	}

	h.refused(400, "INVALID_REASON", h.owner(), "POST", "/api/v1/policies/"+id+"/deactivate", map[string]any{"project_id": h.project, "reason": " "})
	off := h.ok(200, h.owner(), "POST", "/api/v1/policies/"+id+"/deactivate", map[string]any{"project_id": h.project, "reason": "replaced"})
	if off["active"] != nil || off["activated_by"] != nil {
		t.Fatalf("deactivated %v", off)
	}
	h.ok(200, h.owner(), "POST", "/api/v1/policies/"+id+"/deactivate", map[string]any{"project_id": h.project, "reason": "again"})

	for tool, n := range map[string]int{"": 1, "refund_payment": 1, "send_email": 0} {
		path := h.q("/api/v1/policies")
		if tool != "" {
			path += "&tool=" + tool
		}
		if got := h.ok(200, h.viewer(), "GET", path, nil)["items"].([]any); len(got) != n {
			t.Errorf("policies for %q: %d", tool, len(got))
		}
	}
	want := []string{"tool_endpoint.registered", "policy.created", "policy.version_created", "policy.version_created",
		"policy.activated", "policy.activated", "policy.deactivated"}
	if got := h.audits(); !slices.Equal(got, want) {
		t.Fatalf("audit %v", got)
	}
}

func TestPolicyTesting(t *testing.T) {
	h := newHarness(t)
	draft := map[string]any{"project_id": h.project, "document": refundPolicy}
	// The tool is not registered: tests run, activation is reported blocked
	// only for fail_open (this policy fails closed).
	report := h.ok(200, h.engineer(), "POST", "/api/v1/policies/test", draft)
	if report["passed"] != true || report["tool_risk"] != nil || report["activatable"] != true ||
		len(report["results"].([]any)) != 5 || len(report["undecided"].([]any)) != 0 {
		t.Fatalf("report %v", report)
	}
	// Around each threshold, from the first test's action.
	probes := map[string]string{}
	for _, b := range report["boundaries"].([]any) {
		m := b.(map[string]any)
		var effects []string
		for _, p := range m["probes"].([]any) {
			pm := p.(map[string]any)
			effects = append(effects, fmt.Sprintf("%v:%v", pm["value"], pm["effect"]))
		}
		probes[fmt.Sprintf("%s %v", m["path"], m["value"])] = strings.Join(effects, " ")
	}
	if probes["args.amount 100"] != "99:allow 99.99:allow 100:allow 100.01:require_approval 101:require_approval" ||
		probes["args.amount 500"] != "499:require_approval 499.99:require_approval 500:require_approval 500.01:deny 501:deny" ||
		probes["trace.calls 1"] != "0:allow 1:deny 2:deny" {
		t.Fatalf("boundaries %v", probes)
	}

	// Extra cases and a base action; a failing case is explained.
	draft["cases"] = []map[string]any{
		{"name": "refund at the limit", "args": map[string]any{"order_id": "ORD-9", "amount": 100}, "expect": "allow"},
		{"name": "wrong guess", "args": map[string]any{"order_id": "ORD-9", "amount": 120}, "expect": "allow"},
	}
	draft["base"] = map[string]any{"args": map[string]any{"order_id": "ORD-7", "amount": 10}, "context": map[string]any{"traceCalls": 0}}
	report = h.ok(200, h.engineer(), "POST", "/api/v1/policies/test", draft)
	results := report["results"].([]any)
	last := results[len(results)-1].(map[string]any)
	if report["passed"] != false || last["passed"] != false ||
		last["why"] != "expected allow, the policy decided require_approval (Refunds above 100 need a person's approval.)" {
		t.Fatalf("failing case %v", last)
	}
	// Boundaries are probed from the base action: a second refund in the
	// conversation is denied at every amount.
	draft["base"] = map[string]any{"args": map[string]any{"order_id": "ORD-7", "amount": 10}, "context": map[string]any{"traceCalls": 1}}
	report = h.ok(200, h.engineer(), "POST", "/api/v1/policies/test", draft)
	for _, b := range report["boundaries"].([]any) {
		m := b.(map[string]any)
		if m["path"] != "args.amount" {
			continue
		}
		for _, p := range m["probes"].([]any) {
			if pm := p.(map[string]any); pm["effect"] != "deny" || pm["rule"] != "one-refund-per-conversation" {
				t.Fatalf("probe from the base %v", m)
			}
		}
	}

	// A saved version, against the registered tool: fail_open is refused.
	h.register("refund_payment", "WRITE_IRREVERSIBLE", nil)
	open := strings.Replace(refundPolicy, "  rules:", "  failMode: fail_open\n  rules:", 1)
	id := h.createPolicy(open)
	report = h.ok(200, h.reviewer(), "POST", "/api/v1/policies/test", map[string]any{"project_id": h.project, "policy_id": id, "version": 1})
	if report["tool_risk"] != "WRITE_IRREVERSIBLE" || report["activatable"] != false ||
		!strings.Contains(fmt.Sprint(report["activation_problems"]), "fail_open is allowed only for a READ tool") {
		t.Fatalf("saved %v", report)
	}
	h.refused(422, "POLICY_NOT_ACTIVATABLE", h.owner(), "POST", "/api/v1/policies/"+id+"/activate", map[string]any{"project_id": h.project, "version": 1})

	for name, body := range map[string]map[string]any{
		"neither":    {"project_id": h.project},
		"both":       {"project_id": h.project, "document": refundPolicy, "policy_id": id, "version": 1},
		"no version": {"project_id": h.project, "policy_id": id},
		"case":       {"project_id": h.project, "document": refundPolicy, "cases": []map[string]any{{"name": "", "expect": "allow"}}},
		"expect":     {"project_id": h.project, "document": refundPolicy, "cases": []map[string]any{{"name": "x", "expect": "maybe"}}},
	} {
		if r := h.do(h.engineer(), "POST", "/api/v1/policies/test", body, nil); r.status != 400 {
			t.Errorf("%s: %d %s", name, r.status, r.body)
		}
	}
	h.refused(404, "NOT_FOUND", h.engineer(), "POST", "/api/v1/policies/test", map[string]any{"project_id": h.project, "policy_id": id, "version": 4})
	if r := h.do(h.viewer(), "POST", "/api/v1/policies/test", map[string]any{"project_id": h.project, "document": refundPolicy}, nil); r.status != 403 {
		t.Fatalf("a viewer cannot test: %d", r.status)
	}
}
