package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/policy"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
)

// policyCache holds compiled policy versions by id. A version never changes,
// so an entry never goes stale; the cache is bounded by dropping everything
// when it grows past its size (versions are few and cheap to compile).
type policyCache struct {
	mu sync.Mutex
	m  map[string]*policy.Compiled
}

const policyCacheSize = 1000

// compiled returns the compiled version, compiling its stored spec once.
func (c *policyCache) compiled(versionID string, spec json.RawMessage) (*policy.Compiled, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if p, ok := c.m[versionID]; ok {
		return p, nil
	}
	s, err := policy.Decode(spec)
	if err != nil {
		return nil, fmt.Errorf("stored policy version %s: %w", versionID, err)
	}
	p, err := policy.Compile(s)
	if err != nil {
		return nil, fmt.Errorf("stored policy version %s: %w", versionID, err)
	}
	if c.m == nil || len(c.m) >= policyCacheSize {
		c.m = map[string]*policy.Compiled{}
	}
	c.m[versionID] = p
	return p, nil
}

// maxPolicyBody bounds a request carrying a policy document (the document
// itself is bounded by the policy package).
const maxPolicyBody = 512 << 10

// invalidPolicy reports what is wrong with a document.
func invalidPolicy(err error) error {
	var inv *policy.InvalidError
	if errors.As(err, &inv) {
		return httpx.Invalid("INVALID_POLICY", "The policy document is not valid: "+inv.Problems[0].Message+".",
			map[string]any{"problems": inv.Problems})
	}
	return err
}

// readPolicy parses and compiles a document.
func readPolicy(doc string) (*policy.Compiled, error) {
	s, err := policy.Parse([]byte(doc))
	if err != nil {
		return nil, invalidPolicy(err)
	}
	c, err := policy.Compile(s)
	if err != nil {
		return nil, invalidPolicy(err)
	}
	return c, nil
}

// versionJSON is a policy version as the API shows it.
type versionJSON struct {
	store.Version
	Active bool `json:"active"`
}

// versionDetail adds what a person needs to read a version: the variables
// its rules can use and the thresholds they compare against.
type versionDetail struct {
	versionJSON
	Variables  []string           `json:"variables"`
	Thresholds []policy.Threshold `json:"thresholds"`
}

type versionSummary struct {
	ID        string    `json:"id"`
	Version   int       `json:"version"`
	SpecHash  string    `json:"spec_hash"`
	CreatedBy string    `json:"created_by"`
	CreatedAt time.Time `json:"created_at"`
	Active    bool      `json:"active"`
}

type policyDetail struct {
	store.Policy
	Versions []versionSummary `json:"versions"`
}

func isActive(p store.Policy, versionID string) bool {
	return p.Active != nil && p.Active.ID == versionID
}

func newVersion(policyID string, number int, doc string, c *policy.Compiled, by string, s *Server) store.Version {
	spec, err := json.Marshal(c.Spec)
	if err != nil {
		panic(fmt.Sprintf("policy: marshal: %v", err))
	}
	return store.Version{ID: ids.New(), PolicyID: policyID, Version: number, Document: doc, Spec: spec,
		SpecHash: c.Hash, CreatedBy: by, CreatedAt: s.now()}
}

// ---------------------------------------------------------------- list, read

func (s *Server) listPolicies(w http.ResponseWriter, r *http.Request) error {
	q := r.URL.Query()
	_, sc, err := projectOf(r, q.Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	tool := q.Get("tool")
	if tool != "" && !toolName.MatchString(tool) {
		return invalidField("tool", "tool must be a tool name.")
	}
	list, err := s.Store.Policies(r.Context(), sc, tool)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, httpx.Page[store.Policy]{Items: list})
	return nil
}

func (s *Server) getPolicy(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "policy_id")
	if err != nil {
		return err
	}
	_, sc, err := projectOf(r, r.URL.Query().Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	p, err := s.Store.Policy(r.Context(), s.Store.Pool, sc, id, false)
	if err != nil {
		return found(err, "The policy")
	}
	vs, err := s.Store.Versions(r.Context(), p.ID)
	if err != nil {
		return err
	}
	out := policyDetail{Policy: p, Versions: make([]versionSummary, len(vs))}
	for i, v := range vs {
		out.Versions[i] = versionSummary{ID: v.ID, Version: v.Version, SpecHash: v.SpecHash, CreatedBy: v.CreatedBy,
			CreatedAt: v.CreatedAt, Active: isActive(p, v.ID)}
	}
	httpx.WriteJSON(w, http.StatusOK, out)
	return nil
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "policy_id")
	if err != nil {
		return err
	}
	number, err := strconv.Atoi(r.PathValue("version"))
	if err != nil || number < 1 {
		return invalidField("version", "version must be a positive integer.")
	}
	_, sc, err := projectOf(r, r.URL.Query().Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	p, err := s.Store.Policy(r.Context(), s.Store.Pool, sc, id, false)
	if err != nil {
		return found(err, "The policy")
	}
	v, err := s.Store.Version(r.Context(), s.Store.Pool, p.ID, number)
	if err != nil {
		return found(err, "The policy version")
	}
	c, err := s.policies.compiled(v.ID, v.Spec)
	if err != nil {
		return err
	}
	thresholds := c.Thresholds()
	if thresholds == nil {
		thresholds = []policy.Threshold{}
	}
	httpx.WriteJSON(w, http.StatusOK, versionDetail{versionJSON: versionJSON{Version: v, Active: isActive(p, v.ID)},
		Variables: policy.Variables, Thresholds: thresholds})
	return nil
}

// ---------------------------------------------------------------- write

type documentBody struct {
	ProjectID string `json:"project_id"`
	Document  string `json:"document"`
}

func (s *Server) createPolicy(w http.ResponseWriter, r *http.Request) error {
	var b documentBody
	if err := httpx.DecodeJSON(w, r, &b, maxPolicyBody); err != nil {
		return err
	}
	p, sc, err := projectOf(r, b.ProjectID, authn.PermPolicyWrite)
	if err != nil {
		return err
	}
	c, err := readPolicy(b.Document)
	if err != nil {
		return err
	}
	meta := c.Spec.Metadata
	pol := store.Policy{ID: ids.New(), Name: meta.Name, Tool: c.Spec.Spec.Tool, Description: meta.Description,
		LatestVersion: 1, CreatedBy: p.Actor, CreatedAt: s.now(), UpdatedAt: s.now()}
	v := newVersion(pol.ID, 1, b.Document, c, p.Actor, s)
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		if err := s.Store.CreatePolicy(r.Context(), tx, sc, pol, v); err != nil {
			return err
		}
		return s.audit(r.Context(), tx, p, sc.ProjectID, "policy.created", "policy", pol.ID, "",
			map[string]any{"name": pol.Name, "tool": pol.Tool, "version": 1, "spec_hash": v.SpecHash})
	})
	if errors.Is(err, store.ErrConflict) {
		return httpx.NewError(http.StatusConflict, "POLICY_EXISTS",
			fmt.Sprintf("A policy named %q exists in this project; add a version to it instead.", pol.Name)).
			WithDetails(map[string]any{"field": "metadata.name"})
	}
	if err != nil {
		return err
	}
	created, err := s.Store.Policy(r.Context(), s.Store.Pool, sc, pol.ID, false)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusCreated, map[string]any{"policy": created, "version": versionJSON{Version: v}})
	return nil
}

func (s *Server) addVersion(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "policy_id")
	if err != nil {
		return err
	}
	var b documentBody
	if err := httpx.DecodeJSON(w, r, &b, maxPolicyBody); err != nil {
		return err
	}
	p, sc, err := projectOf(r, b.ProjectID, authn.PermPolicyWrite)
	if err != nil {
		return err
	}
	c, err := readPolicy(b.Document)
	if err != nil {
		return err
	}
	var v store.Version
	var pol store.Policy
	created := false
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		var err error
		pol, err = s.Store.Policy(r.Context(), tx, sc, id, true)
		if err != nil {
			return err
		}
		// A version changes a policy's rules, not what it is: another name or
		// tool is another policy.
		if c.Spec.Metadata.Name != pol.Name {
			return httpx.Invalid("INVALID_POLICY", fmt.Sprintf("metadata.name must stay %q.", pol.Name),
				map[string]any{"problems": []policy.Problem{{Field: "metadata.name", Message: "must stay " + strconv.Quote(pol.Name)}}})
		}
		if c.Spec.Spec.Tool != pol.Tool {
			return httpx.Invalid("INVALID_POLICY", fmt.Sprintf("spec.tool must stay %q; guard another tool with another policy.", pol.Tool),
				map[string]any{"problems": []policy.Problem{{Field: "spec.tool", Message: "must stay " + strconv.Quote(pol.Tool)}}})
		}
		latest, err := s.Store.Version(r.Context(), tx, pol.ID, pol.LatestVersion)
		if err != nil {
			return err
		}
		if latest.SpecHash == c.Hash {
			v = latest
			return nil
		}
		created = true
		v = newVersion(pol.ID, pol.LatestVersion+1, b.Document, c, p.Actor, s)
		pol.Description = c.Spec.Metadata.Description
		if err := s.Store.AddVersion(r.Context(), tx, pol, v); err != nil {
			return err
		}
		return s.audit(r.Context(), tx, p, sc.ProjectID, "policy.version_created", "policy", pol.ID, "",
			map[string]any{"name": pol.Name, "tool": pol.Tool, "version": v.Version, "spec_hash": v.SpecHash})
	})
	if err != nil {
		return found(err, "The policy")
	}
	pol, err = s.Store.Policy(r.Context(), s.Store.Pool, sc, id, false)
	if err != nil {
		return err
	}
	status := http.StatusCreated
	if !created {
		// The document decides exactly what the latest version decides.
		status = http.StatusOK
	}
	httpx.WriteJSON(w, status, map[string]any{"policy": pol, "version": versionJSON{Version: v, Active: isActive(pol, v.ID)},
		"created": created})
	return nil
}

// ---------------------------------------------------------------- test

type testBody struct {
	ProjectID string        `json:"project_id"`
	Document  string        `json:"document"`
	PolicyID  string        `json:"policy_id"`
	Version   int           `json:"version"`
	Cases     []policy.Test `json:"cases"`
	// Base is the action the boundaries start from (default: the first
	// test's, else no arguments).
	Base *testAction `json:"base"`
}

// testAction is an action to decide: its arguments and context.
type testAction struct {
	Args    map[string]any `json:"args"`
	Context policy.Context `json:"context"`
}

type testResponse struct {
	Tool               string              `json:"tool"`
	SpecHash           string              `json:"spec_hash"`
	Passed             bool                `json:"passed"`
	Results            []policy.TestResult `json:"results"`
	Boundaries         []policy.Boundary   `json:"boundaries"`
	Undecided          []string            `json:"undecided"`
	ToolRisk           *string             `json:"tool_risk"`
	Activatable        bool                `json:"activatable"`
	ActivationProblems []policy.Problem    `json:"activation_problems"`
}

// checkCases validates extra cases the way a document's tests are.
func checkCases(cases []policy.Test) error {
	if len(cases) > policy.MaxTests {
		return httpx.Invalid("INVALID_CASES", fmt.Sprintf("At most %d cases.", policy.MaxTests), map[string]any{"field": "cases"})
	}
	for i := range cases {
		c := &cases[i]
		field := fmt.Sprintf("cases[%d]", i)
		if strings.TrimSpace(c.Name) == "" || len(c.Name) > 200 {
			return httpx.Invalid("INVALID_CASES", "A case has a name of 1 to 200 characters.", map[string]any{"field": field + ".name"})
		}
		if !c.Expect.Valid() {
			return httpx.Invalid("INVALID_CASES", "expect must be one of allow, allow_with_limits, require_approval, deny.",
				map[string]any{"field": field + ".expect"})
		}
		if c.Args == nil {
			c.Args = map[string]any{}
		}
	}
	return nil
}

func (s *Server) testPolicy(w http.ResponseWriter, r *http.Request) error {
	var b testBody
	if err := httpx.DecodeJSON(w, r, &b, maxPolicyBody); err != nil {
		return err
	}
	_, sc, err := projectOf(r, b.ProjectID, authn.PermPolicyTest)
	if err != nil {
		return err
	}
	if err := checkCases(b.Cases); err != nil {
		return err
	}
	var c *policy.Compiled
	switch {
	case b.Document != "" && b.PolicyID != "":
		return httpx.Invalid("INVALID_PARAMETER", "Test a document or a saved version, not both.", map[string]any{"field": "document"})
	case b.Document != "":
		if c, err = readPolicy(b.Document); err != nil {
			return err
		}
	case b.PolicyID != "":
		id := strings.ToLower(b.PolicyID)
		if !ids.Valid(id) {
			return invalidField("policy_id", "policy_id must be a UUID.")
		}
		if b.Version < 1 {
			return invalidField("version", "version is required with policy_id.")
		}
		p, err := s.Store.Policy(r.Context(), s.Store.Pool, sc, id, false)
		if err != nil {
			return found(err, "The policy")
		}
		v, err := s.Store.Version(r.Context(), s.Store.Pool, p.ID, b.Version)
		if err != nil {
			return found(err, "The policy version")
		}
		if c, err = s.policies.compiled(v.ID, v.Spec); err != nil {
			return err
		}
	default:
		return httpx.Invalid("INVALID_PARAMETER", "Give a document, or a policy_id and version.", map[string]any{"field": "document"})
	}
	tool := c.Spec.Spec.Tool
	var risk *string
	if e, err := s.Store.Endpoint(r.Context(), s.Store.Pool, sc, tool); err == nil {
		risk = &e.Risk
	} else if !errors.Is(err, store.ErrNotFound) {
		return err
	}
	base := policy.Input{Tool: tool, Args: map[string]any{}}
	switch {
	case b.Base != nil:
		if b.Base.Args == nil {
			b.Base.Args = map[string]any{}
		}
		base = policy.Test{Args: b.Base.Args, Context: b.Base.Context}.Input(tool)
	case len(c.Spec.Spec.Tests) > 0:
		base = c.Spec.Spec.Tests[0].Input(tool)
	}
	if base.Risk == "" && risk != nil {
		base.Risk = *risk
	}
	out := testResponse{Tool: tool, SpecHash: c.Hash, Passed: true, Results: c.RunTests(b.Cases),
		Boundaries: c.Boundaries(base), Undecided: c.Undecided(), ToolRisk: risk}
	for _, res := range out.Results {
		out.Passed = out.Passed && res.Passed
	}
	toolRisk := ""
	if risk != nil {
		toolRisk = *risk
	}
	out.ActivationProblems = c.ActivationProblems(toolRisk)
	out.Activatable = len(out.ActivationProblems) == 0
	if out.Boundaries == nil {
		out.Boundaries = []policy.Boundary{}
	}
	if out.Undecided == nil {
		out.Undecided = []string{}
	}
	if out.ActivationProblems == nil {
		out.ActivationProblems = []policy.Problem{}
	}
	httpx.WriteJSON(w, http.StatusOK, out)
	return nil
}

// ---------------------------------------------------------------- activation

type activateBody struct {
	ProjectID string `json:"project_id"`
	Version   int    `json:"version"`
	Reason    string `json:"reason"`
}

func (s *Server) activate(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "policy_id")
	if err != nil {
		return err
	}
	var b activateBody
	if err := httpx.DecodeJSON(w, r, &b, 16<<10); err != nil {
		return err
	}
	p, sc, err := projectOf(r, b.ProjectID, authn.PermPolicyActivate)
	if err != nil {
		return err
	}
	if b.Version < 1 {
		return invalidField("version", "version is required.")
	}
	if len([]rune(b.Reason)) > 2000 {
		return httpx.Invalid("INVALID_REASON", "A reason is at most 2000 characters.", map[string]any{"field": "reason"})
	}
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		pol, err := s.Store.Policy(r.Context(), tx, sc, id, true)
		if err != nil {
			return found(err, "The policy")
		}
		v, err := s.Store.Version(r.Context(), tx, pol.ID, b.Version)
		if err != nil {
			return found(err, "The policy version")
		}
		if isActive(pol, v.ID) {
			return nil
		}
		c, err := s.policies.compiled(v.ID, v.Spec)
		if err != nil {
			return err
		}
		risk := ""
		if e, err := s.Store.Endpoint(r.Context(), tx, sc, pol.Tool); err == nil {
			risk = e.Risk
		} else if !errors.Is(err, store.ErrNotFound) {
			return err
		}
		if ps := c.ActivationProblems(risk); len(ps) > 0 {
			return httpx.NewError(http.StatusUnprocessableEntity, "POLICY_NOT_ACTIVATABLE",
				fmt.Sprintf("Version %d of %s cannot be activated: %s.", v.Version, pol.Name, ps[0].Message)).
				WithDetails(map[string]any{"problems": ps})
		}
		now := s.now()
		if err := s.Store.Activate(r.Context(), tx, pol.ID, &v.ID, p.Actor, now); err != nil {
			return err
		}
		if err := emit(r.Context(), tx, "policy.activated.v1", sc.OrgID, sc.ProjectID, map[string]any{
			"policy_id": pol.ID, "policy_version_id": v.ID, "name": pol.Name, "version": v.Version,
			"target_tool": pol.Tool, "spec_hash": v.SpecHash, "activated_by": p.Actor,
		}); err != nil {
			return err
		}
		meta := map[string]any{"name": pol.Name, "tool": pol.Tool, "version": v.Version, "spec_hash": v.SpecHash,
			"previous_version": nil}
		if pol.Active != nil {
			meta["previous_version"] = pol.Active.Version
		}
		return s.audit(r.Context(), tx, p, sc.ProjectID, "policy.activated", "policy", pol.ID, b.Reason, meta)
	})
	if err != nil {
		return err
	}
	return s.writePolicy(w, r, sc, id)
}

func (s *Server) deactivate(w http.ResponseWriter, r *http.Request) error {
	id, err := pathID(r, "policy_id")
	if err != nil {
		return err
	}
	var b decideBody
	if err := httpx.DecodeJSON(w, r, &b, 16<<10); err != nil {
		return err
	}
	p, sc, err := projectOf(r, b.ProjectID, authn.PermPolicyActivate)
	if err != nil {
		return err
	}
	reason, err := reasonOf(b.Reason)
	if err != nil {
		return err
	}
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		pol, err := s.Store.Policy(r.Context(), tx, sc, id, true)
		if err != nil {
			return found(err, "The policy")
		}
		if pol.Active == nil {
			return nil
		}
		if err := s.Store.Activate(r.Context(), tx, pol.ID, nil, p.Actor, s.now()); err != nil {
			return err
		}
		return s.audit(r.Context(), tx, p, sc.ProjectID, "policy.deactivated", "policy", pol.ID, reason,
			map[string]any{"name": pol.Name, "tool": pol.Tool, "version": pol.Active.Version})
	})
	if err != nil {
		return err
	}
	return s.writePolicy(w, r, sc, id)
}

func (s *Server) writePolicy(w http.ResponseWriter, r *http.Request, sc store.Scope, id string) error {
	pol, err := s.Store.Policy(r.Context(), s.Store.Pool, sc, id, false)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, pol)
	return nil
}
