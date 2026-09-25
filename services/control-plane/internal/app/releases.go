package app

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/changes"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/gate"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/release"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// Bounds on releases and overrides.
const (
	maxCIURL             = 2048
	minOverrideReason    = 10
	maxOverrideReason    = 2000
	maxOverrideDays      = 90
	caseFetchConcurrency = 8
)

// Gate states besides a decision's outcome.
const (
	GatePending    = "PENDING"
	GateOverridden = "OVERRIDDEN"
)

// Evaluation statuses.
const (
	evaluating = "EVALUATING"
	decided    = "DECIDED"
)

// CreateReleaseInput names a candidate version of an agent and its baseline.
type CreateReleaseInput struct {
	ProjectID        string             `json:"project_id"`
	Agent            string             `json:"agent"`
	BaselineVersion  string             `json:"baseline_version"`
	CandidateVersion string             `json:"candidate_version"`
	Title            string             `json:"title,omitempty"`
	Git              *changes.Git       `json:"git,omitempty"`
	Declared         []changes.Declared `json:"declared,omitempty"`
	CIURL            string             `json:"ci_url,omitempty"`
	// Evaluate starts the first evaluation at once (the default).
	Evaluate *bool `json:"evaluate,omitempty"`
}

// OverrideInput lets a gated release through.
type OverrideInput struct {
	Reason    string     `json:"reason"`
	TicketURL string     `json:"ticket_url,omitempty"`
	ExpiresAt *time.Time `json:"expires_at,omitempty"`
	// Revision, when given, must be the release's latest revision.
	Revision int `json:"revision,omitempty"`
}

// GateSummary is a revision's gate as lists show it.
type GateSummary struct {
	Revision         int             `json:"revision"`
	Status           string          `json:"status"`
	Outcome          *string         `json:"outcome"`
	EffectiveOutcome string          `json:"effective_outcome"`
	Incomplete       *bool           `json:"incomplete"`
	RiskIndex        *int            `json:"risk_index"`
	Summary          json.RawMessage `json:"summary"`
	RequestedBy      string          `json:"requested_by"`
	RequestedAt      time.Time       `json:"requested_at"`
	DecidedAt        *time.Time      `json:"decided_at"`
	EvalRunID        *string         `json:"eval_run_id"`
	Overridden       bool            `json:"overridden"`
}

// ReleaseView is a release with the gate of its latest revision.
type ReleaseView struct {
	store.Release
	Gate *GateSummary `json:"gate"`
}

// ReleaseDetail is a release and all its revisions, newest first.
type ReleaseDetail struct {
	Release   ReleaseView   `json:"release"`
	Revisions []GateSummary `json:"revisions"`
}

// OverrideView is an override and whether it still applies.
type OverrideView struct {
	store.Override
	Active bool `json:"active"`
}

// Gate is one revision's gate: what it rests on, the decision, the override,
// and what it means for CI.
type Gate struct {
	ReleaseID    string          `json:"release_id"`
	EvaluationID string          `json:"release_evaluation_id"`
	Revision     int             `json:"revision"`
	Status       string          `json:"status"`
	RequestedBy  string          `json:"requested_by"`
	RequestedAt  time.Time       `json:"requested_at"`
	DecidedAt    *time.Time      `json:"decided_at"`
	EvalRunID    *string         `json:"eval_run_id"`
	Policy       json.RawMessage `json:"policy"`
	Suite        json.RawMessage `json:"suite"`
	Impact       json.RawMessage `json:"impact"`
	// Decision is the gate's decision (package gate), once decided; it never
	// changes, whatever overrides it.
	Decision       json.RawMessage `json:"decision"`
	Summary        json.RawMessage `json:"summary"`
	EvidenceSHA256 *string         `json:"evidence_sha256"`
	// EvidenceVerified says whether the stored decision and input still hash
	// to EvidenceSHA256 (spec §91).
	EvidenceVerified *bool         `json:"evidence_verified"`
	Override         *OverrideView `json:"override"`
	// EffectiveOutcome is PENDING while evaluating, OVERRIDDEN while an
	// override applies, otherwise the decision's outcome.
	EffectiveOutcome string `json:"effective_outcome"`
	ExitCode         *int   `json:"exit_code"`
	CIFails          *bool  `json:"ci_fails"`
}

// CreatedRelease is a new release and, when evaluated at once, its gate.
type CreatedRelease struct {
	Release ReleaseView `json:"release"`
	Gate    *Gate       `json:"gate"`
}

// DecisionSummary is what a decision's list entry says without reading it.
type DecisionSummary struct {
	Scenarios           int           `json:"scenarios"`
	Evaluated           int           `json:"evaluated"`
	NewCriticalFailures int           `json:"new_critical_failures"`
	Regressed           int           `json:"regressed"`
	Failed              int           `json:"failed"`
	Rules               []string      `json:"rules"`
	ExitCode            int           `json:"exit_code"`
	CIFails             bool          `json:"ci_fails"`
	EvalRunStatus       *string       `json:"eval_run_status"`
	Cost                *release.Cost `json:"cost"`
	CostDeltaUSD        *float64      `json:"cost_delta_usd"`
	LatencyP95DeltaMS   *float64      `json:"latency_p95_delta_ms"`
}

// ---------------------------------------------------------------- views

func active(o *store.Override, now time.Time) bool {
	return o != nil && (o.ExpiresAt == nil || o.ExpiresAt.After(now))
}

func effective(r store.Revision, now time.Time) string {
	switch {
	case r.Decision == nil:
		return GatePending
	case active(r.Override, now):
		return GateOverridden
	}
	return r.Decision.Outcome
}

func gateSummary(r store.Revision, now time.Time) GateSummary {
	g := GateSummary{Revision: r.Revision, Status: r.Status, EffectiveOutcome: effective(r, now), RequestedBy: r.RequestedBy,
		RequestedAt: r.RequestedAt, DecidedAt: r.DecidedAt, EvalRunID: r.EvalRunID, Summary: json.RawMessage("null"),
		Overridden: r.Override != nil}
	if d := r.Decision; d != nil {
		g.Outcome, g.Incomplete, g.RiskIndex, g.Summary = &d.Outcome, &d.Incomplete, &d.RiskIndex, d.Summary
	}
	return g
}

func (a *App) releaseView(r store.Release, latest *store.Revision) ReleaseView {
	v := ReleaseView{Release: r}
	if latest != nil {
		g := gateSummary(*latest, a.Now())
		v.Gate = &g
	}
	return v
}

// releaseFor returns a release of a project the caller may act on with perm.
func (a *App) releaseFor(ctx context.Context, p authn.Principal, perm authn.Permission, id string) (store.Release, error) {
	if err := require(p, perm); err != nil {
		return store.Release{}, err
	}
	r, err := a.Store.GetRelease(ctx, a.Store.Pool, p.OrgID, id)
	if err != nil {
		return store.Release{}, notFoundOr(err)
	}
	if !p.CanAccessProject(r.ProjectID) {
		return store.Release{}, httpx.ErrNotFound
	}
	return r, nil
}

// ---------------------------------------------------------------- create, read

func invalidRelease(msg, field string) error {
	return httpx.Invalid("INVALID_RELEASE", msg, map[string]any{"field": field})
}

func checkURL(raw string, max int) bool {
	u, err := url.Parse(raw)
	return err == nil && len(raw) <= max && (u.Scheme == "https" || u.Scheme == "http") && u.Host != "" &&
		!hasControl(raw)
}

// CreateRelease records a candidate version of an agent against its
// baseline (spec §38): the change set between them, and — unless asked not
// to — the first evaluation. Requires release.write.
func (a *App) CreateRelease(ctx context.Context, p authn.Principal, in CreateReleaseInput) (CreatedRelease, error) {
	if !ids.Valid(in.ProjectID) {
		return CreatedRelease{}, invalidRelease("project_id must name a project.", "project_id")
	}
	if _, err := a.projectFor(ctx, p, authn.PermReleaseWrite, in.ProjectID); err != nil {
		return CreatedRelease{}, err
	}
	in.CIURL = strings.TrimSpace(in.CIURL)
	if in.CIURL != "" && !checkURL(in.CIURL, maxCIURL) {
		return CreatedRelease{}, invalidRelease("ci_url must be an http(s) URL of at most 2048 characters.", "ci_url")
	}
	cs, err := a.CreateChangeSet(ctx, p, in.ProjectID, CreateChangeSetInput{Agent: in.Agent, BaseVersion: in.BaselineVersion,
		CandidateVersion: in.CandidateVersion, Title: in.Title, Git: in.Git, Declared: in.Declared})
	if err != nil {
		var he *httpx.Error
		if errors.As(err, &he) && he.Code == "INVALID_CHANGE_SET" {
			field, _ := he.Details["field"].(string)
			field = strings.NewReplacer("base_version", "baseline_version").Replace(field)
			return CreatedRelease{}, invalidRelease(he.Message, field)
		}
		return CreatedRelease{}, err
	}
	var commit, ciURL *string
	var git changes.Git
	if len(cs.Git) > 0 && json.Unmarshal(cs.Git, &git) == nil && git.CandidateCommit != "" {
		commit = &git.CandidateCommit
	}
	if in.CIURL != "" {
		ciURL = &in.CIURL
	}
	id := newID()
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		if err := a.Store.InsertRelease(ctx, tx, store.NewRelease{ID: id, OrganizationID: p.OrgID, ProjectID: in.ProjectID,
			AgentID: cs.AgentID, ChangeSetID: cs.ID, BaselineVersionID: cs.Base.ID, CandidateVersionID: cs.Candidate.ID,
			Title: cs.Title, CommitSHA: commit, CIURL: ciURL, CreatedBy: p.Actor}); err != nil {
			return err
		}
		return a.audit(ctx, tx, p, in.ProjectID, "release.created", "release", id, nil,
			map[string]any{"change_set_id": cs.ID, "baseline_version_id": cs.Base.ID, "candidate_version_id": cs.Candidate.ID},
			"", map[string]any{"agent": cs.AgentName, "baseline_version": cs.Base.Version, "candidate_version": cs.Candidate.Version})
	})
	if err != nil {
		return CreatedRelease{}, err
	}
	rel, err := a.Store.GetRelease(ctx, a.Store.Pool, p.OrgID, id)
	if err != nil {
		return CreatedRelease{}, err
	}
	out := CreatedRelease{Release: a.releaseView(rel, nil)}
	if in.Evaluate == nil || *in.Evaluate {
		g, err := a.evaluate(ctx, p, rel)
		if err != nil {
			return CreatedRelease{}, err
		}
		out.Gate = &g
		latest, err := a.Store.EvaluationOf(ctx, a.Store.Pool, p.OrgID, id, 0)
		if err != nil {
			return CreatedRelease{}, err
		}
		out.Release = a.releaseView(rel, &latest.Revision)
	}
	return out, nil
}

// GetRelease returns a release and its revisions.
func (a *App) GetRelease(ctx context.Context, p authn.Principal, id string) (ReleaseDetail, error) {
	rel, err := a.releaseFor(ctx, p, authn.PermRead, id)
	if err != nil {
		return ReleaseDetail{}, err
	}
	revs, err := a.Store.Revisions(ctx, a.Store.Pool, p.OrgID, id)
	if err != nil {
		return ReleaseDetail{}, err
	}
	out := ReleaseDetail{Revisions: []GateSummary{}}
	now := a.Now()
	for _, r := range revs {
		out.Revisions = append(out.Revisions, gateSummary(r, now))
	}
	var latest *store.Revision
	if len(revs) > 0 {
		latest = &revs[0]
	}
	out.Release = a.releaseView(rel, latest)
	return out, nil
}

// ReleasePage selects a page of a project's releases.
type ReleasePage struct {
	Agent  string // optional agent name
	Before *httpx.Cursor
	Limit  int
}

// ListReleases lists a project's releases, newest first, each with the gate
// of its latest revision.
func (a *App) ListReleases(ctx context.Context, p authn.Principal, projectID string, page ReleasePage) ([]ReleaseView, error) {
	if !ids.Valid(projectID) {
		return nil, httpx.Invalid("INVALID_PARAMETER", "project_id must name a project.", map[string]any{"field": "project_id"})
	}
	if _, err := a.projectFor(ctx, p, authn.PermRead, projectID); err != nil {
		return nil, err
	}
	f := store.ReleaseFilter{ProjectID: projectID, Limit: page.Limit}
	if page.Agent != "" {
		if !agentName.MatchString(page.Agent) {
			return nil, httpx.Invalid("INVALID_PARAMETER", "agent must be an agent name.", map[string]any{"field": "agent"})
		}
		ag, err := a.Store.GetAgentByName(ctx, p.OrgID, projectID, page.Agent)
		if errors.Is(err, store.ErrNotFound) {
			return []ReleaseView{}, nil
		}
		if err != nil {
			return nil, err
		}
		f.AgentID = ag.ID
	}
	if page.Before != nil {
		t := page.Before.TS.UTC().Truncate(time.Microsecond)
		f.Before, f.BeforeID = &t, page.Before.ID
	}
	rels, err := a.Store.ListReleases(ctx, p.OrgID, f)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(rels))
	for _, r := range rels {
		ids = append(ids, r.ID)
	}
	latest, err := a.Store.LatestRevisions(ctx, p.OrgID, ids)
	if err != nil {
		return nil, err
	}
	out := make([]ReleaseView, 0, len(rels))
	for _, r := range rels {
		var l *store.Revision
		if rev, ok := latest[r.ID]; ok {
			l = &rev
		}
		out = append(out, a.releaseView(r, l))
	}
	return out, nil
}

// ReleaseGate returns a revision's gate (0: the latest).
func (a *App) ReleaseGate(ctx context.Context, p authn.Principal, id string, revision int) (Gate, error) {
	if _, err := a.releaseFor(ctx, p, authn.PermRead, id); err != nil {
		return Gate{}, err
	}
	return a.gateOf(ctx, p.OrgID, id, revision)
}

func (a *App) gateOf(ctx context.Context, orgID, releaseID string, revision int) (Gate, error) {
	ev, err := a.Store.EvaluationOf(ctx, a.Store.Pool, orgID, releaseID, revision)
	if errors.Is(err, store.ErrNotFound) {
		if revision > 0 {
			return Gate{}, httpx.ErrNotFound
		}
		return Gate{}, httpx.NewError(http.StatusNotFound, "NOT_EVALUATED", "The release has not been evaluated yet.")
	}
	if err != nil {
		return Gate{}, err
	}
	now := a.Now()
	g := Gate{ReleaseID: releaseID, EvaluationID: ev.ID, Revision: ev.Revision.Revision, Status: ev.Status,
		RequestedBy: ev.RequestedBy, RequestedAt: ev.RequestedAt, DecidedAt: ev.DecidedAt, EvalRunID: ev.EvalRunID,
		Policy: ev.Policy, Suite: ev.Suite, Impact: ev.Impact, Decision: json.RawMessage("null"),
		Summary: json.RawMessage("null"), EffectiveOutcome: effective(ev.Revision, now)}
	if ev.Decision == nil {
		return g, nil
	}
	d, err := a.Store.GetDecision(ctx, a.Store.Pool, orgID, ev.Decision.ID)
	if err != nil {
		return Gate{}, err
	}
	g.Decision, g.Summary, g.EvidenceSHA256 = d.Decision, d.Summary, &d.EvidenceSHA256
	verified := evidenceHash(d.Input, d.Decision) == d.EvidenceSHA256
	if !verified {
		a.Log.ErrorContext(ctx, "gate decision evidence does not match its hash", "gate_decision_id", d.ID,
			"release_id", releaseID)
	}
	g.EvidenceVerified = &verified
	var dec struct {
		ExitCode int  `json:"exit_code"`
		CIFails  bool `json:"ci_fails"`
	}
	if err := json.Unmarshal(d.Decision, &dec); err != nil {
		return Gate{}, fmt.Errorf("stored decision %s: %w", d.ID, err)
	}
	g.ExitCode, g.CIFails = &dec.ExitCode, &dec.CIFails
	if o := ev.Override; o != nil {
		g.Override = &OverrideView{Override: *o, Active: active(o, now)}
		if g.Override.Active {
			zero, no := 0, false
			g.ExitCode, g.CIFails = &zero, &no
		}
	}
	return g, nil
}

// ---------------------------------------------------------------- evaluate

// EvaluateRelease starts a new evaluation of a release: a new revision,
// whatever the earlier ones decided (spec §91). Requires release.write.
func (a *App) EvaluateRelease(ctx context.Context, p authn.Principal, id string) (Gate, error) {
	rel, err := a.releaseFor(ctx, p, authn.PermReleaseWrite, id)
	if err != nil {
		return Gate{}, err
	}
	return a.evaluate(ctx, p, rel)
}

// gatePolicy is the part of the resolved gate policy the rules read.
func gatePolicy(r domain.ResolvedGatePolicy) gate.Policy {
	return gate.Policy{LatencyRegressionPct: r.LatencyRegressionPct, LatencyRegressionMinMS: r.LatencyRegressionMinMS,
		CostRegressionPct: r.CostRegressionPct, SemanticRegressionDrop: r.SemanticRegressionDrop,
		JudgeMinAgreement: r.JudgeMinAgreement, WarnFailsCI: r.WarnFailsCI}
}

// evaluationBasis is what an evaluation rests on, stored with it.
type evaluationBasis struct {
	Policy domain.ResolvedGatePolicy
	Impact ChangeImpact
	Suite  []release.Entry
	Tools  []gate.Tool
}

func (b evaluationBasis) problems() []string {
	out := make([]string, 0, len(b.Impact.Problems))
	for _, pr := range b.Impact.Problems {
		out = append(out, fmt.Sprintf("%s: %s (%s)", pr.Service, pr.Message, pr.Code))
	}
	return out
}

// input is the gate's input without the run.
func (b evaluationBasis) input() gate.Input {
	return gate.Input{Policy: gatePolicy(b.Policy), Suite: release.GateSuite(b.Suite),
		Impact: release.GateImpact(b.Impact.Complete, b.problems(), b.Impact.Impact, b.Suite), Tools: b.Tools,
		JudgeAgreement: map[string]float64{}}
}

func basisOf(ev store.Evaluation) (evaluationBasis, error) {
	var b evaluationBasis
	for _, f := range []struct {
		raw json.RawMessage
		dst any
	}{{ev.Policy, &b.Policy}, {ev.Impact, &b.Impact}, {ev.Suite, &b.Suite}, {ev.Tools, &b.Tools}} {
		if err := json.Unmarshal(f.raw, f.dst); err != nil {
			return b, fmt.Errorf("stored basis of release evaluation %s: %w", ev.ID, err)
		}
	}
	return b, nil
}

// seedOf is a release's simulation seed: every evaluation of one release
// runs the same seeded faults, so a rerun is a rerun.
func seedOf(releaseID string) uint32 {
	sum := sha256.Sum256([]byte("release-seed:" + releaseID))
	return binary.BigEndian.Uint32(sum[:4])
}

func agentRef(v store.AgentVersion) map[string]any {
	return map[string]any{"agent": v.AgentName, "version": v.Version, "version_id": v.ID, "manifest_hash": v.ManifestSHA256,
		"prompt_hash": v.PromptSHA256, "endpoint": v.RuntimeEndpoint,
		"model": map[string]any{"provider": v.ModelProvider, "name": v.ModelName}}
}

func (a *App) evaluate(ctx context.Context, p authn.Principal, rel store.Release) (Gate, error) {
	proj, err := a.Store.GetProject(ctx, p.OrgID, rel.ProjectID)
	if err != nil {
		return Gate{}, notFoundOr(err)
	}
	gp, err := domain.ParseGatePolicy(proj.GatePolicy)
	if err != nil {
		return Gate{}, fmt.Errorf("gate policy of project %s: %w", rel.ProjectID, err)
	}
	imp, err := a.ChangeSetImpact(ctx, p, rel.ChangeSetID)
	if err != nil {
		return Gate{}, err
	}
	pool := a.Store.Pool
	base, err := a.Store.GetAgentVersionByID(ctx, pool, p.OrgID, rel.Baseline.ID)
	if err != nil {
		return Gate{}, err
	}
	cand, err := a.Store.GetAgentVersionByID(ctx, pool, p.OrgID, rel.Candidate.ID)
	if err != nil {
		return Gate{}, err
	}
	var manifest domain.Manifest
	if err := json.Unmarshal(cand.Manifest, &manifest); err != nil {
		return Gate{}, fmt.Errorf("stored manifest of %s@%s: %w", cand.AgentName, cand.Version, err)
	}
	b := evaluationBasis{Policy: gp.Resolve(), Impact: imp, Suite: release.Suite(imp.Impact), Tools: []gate.Tool{}}
	for _, t := range manifest.Tools {
		b.Tools = append(b.Tools, gate.Tool{Name: t.Name, Risk: string(t.Risk)})
	}
	var runnable []map[string]any
	for _, e := range b.Suite {
		if !e.Runnable() {
			continue
		}
		reasons := make([]map[string]any, 0, len(e.Why))
		for _, w := range e.Why {
			reasons = append(reasons, map[string]any{"why": w})
		}
		runnable = append(runnable, map[string]any{"scenario_version_id": e.ScenarioVersionID, "scenario_name": e.ScenarioName,
			"severity": e.Severity, "mandatory": e.Mandatory, "known_regression": e.KnownRegression, "reasons": reasons})
	}
	evalID := newID()
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		if err := a.Store.LockRelease(ctx, tx, p.OrgID, rel.ID); err != nil {
			return err
		}
		rev, err := a.Store.NextRevision(ctx, tx, rel.ID)
		if err != nil {
			return err
		}
		if err := a.Store.InsertEvaluation(ctx, tx, store.NewEvaluation{ID: evalID, OrganizationID: p.OrgID,
			ProjectID: rel.ProjectID, ReleaseID: rel.ID, Revision: rev, RequestedBy: p.Actor, Policy: b.Policy,
			Impact: b.Impact, Suite: b.Suite, Tools: b.Tools}); err != nil {
			return err
		}
		meta := map[string]any{"revision": rev, "scenarios": len(b.Suite), "runnable": len(runnable),
			"impact_complete": imp.Complete}
		if err := a.audit(ctx, tx, p, rel.ProjectID, "release.evaluate", "release", rel.ID, nil,
			map[string]any{"release_evaluation_id": evalID, "suite": release.Scenarios(b.Suite)}, "", meta); err != nil {
			return err
		}
		if len(runnable) == 0 {
			// Nothing the simulation service can run: the gate decides now
			// on what is known (an empty suite is a coverage warning; a
			// suite nobody could pin is missing evidence).
			in := b.input()
			if len(b.Suite) > 0 {
				in.RunProblem = "No scenario of the suite could be pinned: the scenario library did not confirm any of them."
			}
			return a.decide(ctx, tx, p, rel, evalID, rev, in, nil)
		}
		return a.emit(ctx, tx, "evaluation.run_requested.v1", p.OrgID, rel.ProjectID, map[string]any{
			"release_evaluation_id": evalID, "release_id": rel.ID, "baseline": agentRef(base), "candidate": agentRef(cand),
			"suite": runnable, "seed": seedOf(rel.ID), "requested_by": p.Actor,
			"budget":       map[string]any{"max_gate_cost_usd": b.Policy.MaxGateCostUSD},
			"judge_policy": map[string]any{"min_agreement": b.Policy.JudgeMinAgreement},
		})
	})
	if err != nil {
		return Gate{}, err
	}
	return a.gateOf(ctx, p.OrgID, rel.ID, 0)
}

// runFacts is what the evaluation service said about the run.
type runFacts struct {
	id     string
	status string
	cost   release.Cost
	detail *release.RunDetail
}

// decide stores an evaluation's decision in tx, once: a revision decided
// before keeps its decision.
func (a *App) decide(ctx context.Context, tx pgx.Tx, p authn.Principal, rel store.Release, evalID string, revision int,
	in gate.Input, run *runFacts) error {
	var runID *string
	if run != nil && run.id != "" {
		runID = &run.id
	}
	moved, err := a.Store.MarkDecided(ctx, tx, p.OrgID, evalID, runID)
	if err != nil || !moved {
		return err
	}
	d := gate.Decide(in)
	sum := DecisionSummary{Scenarios: len(in.Suite), Evaluated: d.Counts.Evaluated,
		NewCriticalFailures: d.Counts.NewCriticalFailures, Regressed: d.Counts.Regressed, Failed: d.Counts.Failed,
		Rules: []string{}, ExitCode: d.ExitCode, CIFails: d.CIFails}
	for _, r := range d.Rules {
		sum.Rules = append(sum.Rules, r.Rule)
	}
	if run != nil {
		sum.EvalRunStatus = &run.status
		cost := run.cost
		sum.Cost = &cost
		if run.detail != nil && run.detail.Summary != nil {
			bs, cs := run.detail.Summary.Baseline, run.detail.Summary.Candidate
			if bs.CostUSD != nil && cs.CostUSD != nil && bs.CostKnown == cs.CostKnown {
				v := *cs.CostUSD - *bs.CostUSD
				sum.CostDeltaUSD = &v
			}
			if bs.LatencyP95MS != nil && cs.LatencyP95MS != nil {
				v := *cs.LatencyP95MS - *bs.LatencyP95MS
				sum.LatencyP95DeltaMS = &v
			}
		}
	}
	sha := evidenceHash(in, d)
	decisionID := newID()
	if err := a.Store.InsertDecision(ctx, tx, store.NewDecision{ID: decisionID, OrganizationID: p.OrgID,
		ProjectID: rel.ProjectID, ReleaseID: rel.ID, EvaluationID: evalID, Outcome: d.Outcome, Incomplete: d.Incomplete,
		RulesVersion: d.RulesVersion, RiskIndex: d.RiskIndex.Value, Decision: d, Input: in, Summary: sum,
		EvidenceSHA256: sha}); err != nil {
		return err
	}
	return a.audit(ctx, tx, p, rel.ProjectID, "release.gate_decided", "release", rel.ID, nil,
		map[string]any{"gate_decision_id": decisionID, "outcome": d.Outcome, "evidence_sha256": sha}, "",
		map[string]any{"revision": revision, "outcome": d.Outcome, "incomplete": d.Incomplete, "rules": sum.Rules,
			"risk_index": d.RiskIndex.Value})
}

// evidenceHash is the identity of a decision and what it rests on: the
// canonical JSON of both, so the stored copies hash the same.
func evidenceHash(input, decision any) string {
	return mustHash(map[string]any{"input": input, "decision": decision})
}

// ---------------------------------------------------------------- the evaluation service's answer

type runCompleted struct {
	EvalRunID           string  `json:"eval_run_id"`
	ReleaseEvaluationID *string `json:"release_evaluation_id"`
	Status              string  `json:"status"`
	Error               *string `json:"error"`
}

func notFound(err error) bool {
	var he *httpx.Error
	return errors.As(err, &he) && he.Status == http.StatusNotFound
}

// HandleEvaluationCompleted decides a release evaluation when the
// evaluation service finished its run (evaluation.run_completed.v1): it
// reads the run, every compared case and the judge's calibrations, and
// stores the decision with everything it rests on. A run that is not a
// release's is none of the gate's business; a redelivered event finds the
// revision decided. A run the evaluation service no longer knows is missing
// evidence (BLOCK); a service that cannot answer now is retried.
func (a *App) HandleEvaluationCompleted(ctx context.Context, env events.Envelope) error {
	var pl runCompleted
	if err := env.Decode(&pl); err != nil {
		return err
	}
	if pl.ReleaseEvaluationID == nil || *pl.ReleaseEvaluationID == "" {
		return nil
	}
	if !ids.Valid(*pl.ReleaseEvaluationID) || !ids.Valid(pl.EvalRunID) {
		return events.Permanent(fmt.Errorf("evaluation.run_completed.v1: malformed ids (eval_run_id %q, release_evaluation_id %q)",
			pl.EvalRunID, *pl.ReleaseEvaluationID))
	}
	pool := a.Store.Pool
	ev, err := a.Store.GetEvaluation(ctx, pool, env.OrganizationID, *pl.ReleaseEvaluationID)
	if errors.Is(err, store.ErrNotFound) {
		a.Log.WarnContext(ctx, "release evaluation not found for a completed run",
			"release_evaluation_id", *pl.ReleaseEvaluationID, "eval_run_id", pl.EvalRunID)
		return nil
	}
	if err != nil {
		return err
	}
	if ev.Status == decided {
		return nil
	}
	rel, err := a.Store.GetRelease(ctx, pool, env.OrganizationID, ev.ReleaseID)
	if err != nil {
		return err
	}
	b, err := basisOf(ev)
	if err != nil {
		return events.Permanent(err)
	}
	p := authn.ServicePrincipal(Producer, env.OrganizationID, ev.ProjectID)
	in := b.input()
	run := &runFacts{id: pl.EvalRunID, status: pl.Status}
	if a.Evaluation == nil {
		in.RunProblem = "The evaluation service is not configured on the control plane; the run could not be read."
	} else if err := a.readRun(ctx, p, ev.ProjectID, ev.ID, pl.EvalRunID, &in, run); err != nil {
		return err
	}
	return a.Store.Tx(ctx, func(tx pgx.Tx) error {
		return a.decide(ctx, tx, p, rel, ev.ID, ev.Revision.Revision, in, run)
	})
}

// readRun fills the gate input with the evaluation service's run: the run,
// its cases' details (a completed run's) and the judge's calibrations.
func (a *App) readRun(ctx context.Context, p authn.Principal, projectID, evaluationID, runID string, in *gate.Input,
	run *runFacts) error {
	var detail release.RunDetail
	err := a.Evaluation.Do(ctx, p, http.MethodGet, "/api/v1/eval-runs/"+url.PathEscape(runID), nil, &detail)
	if notFound(err) {
		in.RunProblem = fmt.Sprintf("The evaluation run %s is not known to the evaluation service.", runID)
		return nil
	}
	if err != nil {
		return fmt.Errorf("read evaluation run %s: %w", runID, err)
	}
	if detail.Run.ReleaseEvaluationID == nil || !strings.EqualFold(*detail.Run.ReleaseEvaluationID, evaluationID) {
		in.RunProblem = fmt.Sprintf("The evaluation run %s was not run for this release evaluation.", runID)
		return nil
	}
	details := map[string]release.CaseDetail{}
	if detail.Run.Status == "COMPLETED" {
		if details, err = a.readCases(ctx, p, runID, detail.Cases); err != nil {
			return err
		}
	}
	var judges release.JudgeDescription
	if err := a.Evaluation.Do(ctx, p, http.MethodGet, "/api/v1/judges?project_id="+url.QueryEscape(projectID), nil,
		&judges); err != nil {
		return fmt.Errorf("read the judge's calibrations: %w", err)
	}
	in.Run = release.GateRun(detail, details)
	in.JudgeAgreement = release.JudgeAgreement(detail.Run.Judge, judges)
	run.status, run.cost, run.detail = detail.Run.Status, release.CostOf(detail), &detail
	return nil
}

// readCases reads every compared case's detail, a few at a time. A case
// the evaluation service no longer has keeps its statuses and loses its
// expectations.
func (a *App) readCases(ctx context.Context, p authn.Principal, runID string, cases []release.CaseSummary) (map[string]release.CaseDetail, error) {
	out := make(map[string]release.CaseDetail, len(cases))
	var mu sync.Mutex
	var first error
	sem := make(chan struct{}, caseFetchConcurrency)
	var wg sync.WaitGroup
	for _, c := range cases {
		wg.Add(1)
		sem <- struct{}{}
		go func(name string) {
			defer wg.Done()
			defer func() { <-sem }()
			var d release.CaseDetail
			err := a.Evaluation.Do(ctx, p, http.MethodGet,
				"/api/v1/eval-runs/"+url.PathEscape(runID)+"/cases/"+url.PathEscape(name), nil, &d)
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				out[name] = d
			case notFound(err):
			case first == nil:
				first = fmt.Errorf("read case %s of evaluation run %s: %w", name, runID, err)
			}
		}(c.ScenarioName)
	}
	wg.Wait()
	return out, first
}

// ---------------------------------------------------------------- override

func overrideRefused(status int, code, msg string) error {
	return httpx.NewError(status, code, msg)
}

// OverrideGate lets a gated release through (spec §92): who, why, until
// when, and the ticket behind it. It never changes the decision: the gate
// still says what it decided, and that it was overridden. One override per
// decision; a new evaluation needs its own. Requires release.override, and
// a reviewer may override only when the project's gate policy allows it.
func (a *App) OverrideGate(ctx context.Context, p authn.Principal, id string, in OverrideInput) (Gate, error) {
	rel, err := a.releaseFor(ctx, p, authn.PermReleaseOverride, id)
	if err != nil {
		return Gate{}, err
	}
	proj, err := a.Store.GetProject(ctx, p.OrgID, rel.ProjectID)
	if err != nil {
		return Gate{}, notFoundOr(err)
	}
	gp, err := domain.ParseGatePolicy(proj.GatePolicy)
	if err != nil {
		return Gate{}, fmt.Errorf("gate policy of project %s: %w", rel.ProjectID, err)
	}
	if p.Role == authn.RoleReviewer && !gp.Resolve().AllowReviewerOverride {
		return Gate{}, overrideRefused(http.StatusForbidden, "OVERRIDE_NOT_ALLOWED",
			"The project's gate policy does not let reviewers override the gate; an owner or an administrator can.")
	}
	in.Reason = strings.TrimSpace(in.Reason)
	in.TicketURL = strings.TrimSpace(in.TicketURL)
	n := utf8.RuneCountInString(in.Reason)
	if n < minOverrideReason || n > maxOverrideReason || strings.ContainsFunc(in.Reason, func(r rune) bool {
		return r != '\n' && r != '\t' && hasControl(string(r))
	}) {
		return Gate{}, httpx.Invalid("INVALID_OVERRIDE", "reason says why, in 10 to 2000 characters.",
			map[string]any{"field": "reason"})
	}
	if in.TicketURL != "" && !checkURL(in.TicketURL, maxCIURL) {
		return Gate{}, httpx.Invalid("INVALID_OVERRIDE", "ticket_url must be an http(s) URL of at most 2048 characters.",
			map[string]any{"field": "ticket_url"})
	}
	now := a.Now()
	if e := in.ExpiresAt; e != nil && (!e.After(now) || e.After(now.Add(maxOverrideDays*24*time.Hour))) {
		return Gate{}, httpx.Invalid("INVALID_OVERRIDE", "expires_at must be in the future and at most 90 days away.",
			map[string]any{"field": "expires_at"})
	}
	if in.Revision < 0 {
		return Gate{}, httpx.Invalid("INVALID_OVERRIDE", "revision is a positive number.", map[string]any{"field": "revision"})
	}
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		if err := a.Store.LockRelease(ctx, tx, p.OrgID, rel.ID); err != nil {
			return err
		}
		ev, err := a.Store.EvaluationOf(ctx, tx, p.OrgID, rel.ID, 0)
		switch {
		case errors.Is(err, store.ErrNotFound):
			return overrideRefused(http.StatusConflict, "NOT_EVALUATED", "The release has not been evaluated; there is no gate to override.")
		case err != nil:
			return err
		case in.Revision != 0 && in.Revision != ev.Revision.Revision:
			return overrideRefused(http.StatusConflict, "NOT_LATEST_REVISION",
				fmt.Sprintf("Only the latest revision (%d) can be overridden.", ev.Revision.Revision))
		case ev.Decision == nil:
			return overrideRefused(http.StatusConflict, "GATE_PENDING", "The evaluation has not decided yet.")
		case ev.Decision.Outcome == gate.Pass:
			return overrideRefused(http.StatusConflict, "NOTHING_TO_OVERRIDE", "The gate passed; there is nothing to override.")
		case ev.Override != nil:
			return overrideRefused(http.StatusConflict, "ALREADY_OVERRIDDEN", "This decision was overridden before.")
		}
		d := ev.Decision
		var ticket *string
		if in.TicketURL != "" {
			ticket = &in.TicketURL
		}
		var expires *time.Time
		if in.ExpiresAt != nil {
			t := in.ExpiresAt.UTC().Truncate(time.Microsecond)
			expires = &t
		}
		oid := newID()
		if err := a.Store.InsertOverride(ctx, tx, store.NewOverride{ID: oid, OrganizationID: p.OrgID,
			ProjectID: rel.ProjectID, ReleaseID: rel.ID, DecisionID: d.ID, OriginalOutcome: d.Outcome, Reason: in.Reason,
			TicketURL: ticket, ExpiresAt: expires, Actor: p.Actor}); err != nil {
			if errors.Is(err, store.ErrConflict) {
				return overrideRefused(http.StatusConflict, "ALREADY_OVERRIDDEN", "This decision was overridden before.")
			}
			return err
		}
		return a.audit(ctx, tx, p, rel.ProjectID, "release.override", "release", rel.ID,
			map[string]any{"gate_decision_id": d.ID, "outcome": d.Outcome},
			map[string]any{"gate_override_id": oid, "effective_outcome": GateOverridden}, in.Reason,
			map[string]any{"revision": ev.Revision.Revision, "original_outcome": d.Outcome, "ticket_url": ticket,
				"expires_at": expires})
	})
	if err != nil {
		return Gate{}, err
	}
	return a.gateOf(ctx, p.OrgID, rel.ID, 0)
}
