package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/svcclient"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/changes"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/impact"
)

// ImpactProblem is a service the impact could not ask.
type ImpactProblem struct {
	Service string `json:"service"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// ChangeImpact is what a change set requires, and what it was computed from.
type ChangeImpact struct {
	ChangeSetID      string        `json:"change_set_id"`
	ProjectID        string        `json:"project_id"`
	Agent            string        `json:"agent"`
	BaseVersion      string        `json:"base_version"`
	CandidateVersion string        `json:"candidate_version"`
	Policy           impact.Policy `json:"policy"`
	// Complete: both services answered in full. An incomplete impact lists
	// what is known and says what is missing (problems, notes).
	Complete   bool            `json:"complete"`
	Problems   []ImpactProblem `json:"problems"`
	ComputedAt time.Time       `json:"computed_at"`
	impact.Impact
}

// ChangeSetImpact computes the scenarios a change set requires (spec §22):
// the graph service's blast radius of its changed components, entered at the
// candidate version, and the simulation service's scenarios — those the
// graph links, those the gate policy always runs, known regressions and
// those semantically close to what changed — each with why. It is computed
// when asked (the graph and the scenario library change); a release gate
// pins what it ran. Requires read access to the change set's project; the
// services are asked on the caller's behalf, for that project only.
func (a *App) ChangeSetImpact(ctx context.Context, p authn.Principal, id string) (ChangeImpact, error) {
	if err := require(p, authn.PermRead); err != nil {
		return ChangeImpact{}, err
	}
	pool := a.Store.Pool
	cs, err := a.Store.GetChangeSet(ctx, pool, p.OrgID, id)
	if err != nil {
		return ChangeImpact{}, notFoundOr(err)
	}
	if !p.CanAccessProject(cs.ProjectID) {
		return ChangeImpact{}, httpx.ErrNotFound
	}
	proj, err := a.Store.GetProject(ctx, p.OrgID, cs.ProjectID)
	if err != nil {
		return ChangeImpact{}, notFoundOr(err)
	}
	gp, err := domain.ParseGatePolicy(proj.GatePolicy)
	if err != nil {
		// Policies are validated when saved: one that no longer parses is ours to fix.
		return ChangeImpact{}, fmt.Errorf("gate policy of project %s: %w", cs.ProjectID, err)
	}
	resolved := gp.Resolve()
	pol := impact.Policy{AlwaysRunTags: resolved.AlwaysRunTags, IncludeKnownRegressions: resolved.IncludeKnownRegressions,
		MaxDepth: resolved.MaxDepth}
	if pol.AlwaysRunTags == nil {
		pol.AlwaysRunTags = []string{}
	}

	var items []changes.Item
	var seeds []changes.Seed
	if err := json.Unmarshal(cs.Items, &items); err != nil {
		return ChangeImpact{}, fmt.Errorf("stored items of change set %s: %w", cs.ID, err)
	}
	if err := json.Unmarshal(cs.Seeds, &seeds); err != nil {
		return ChangeImpact{}, fmt.Errorf("stored seeds of change set %s: %w", cs.ID, err)
	}
	base, err := a.impactVersion(ctx, p.OrgID, cs.Base.ID)
	if err != nil {
		return ChangeImpact{}, err
	}
	cand, err := a.impactVersion(ctx, p.OrgID, cs.Candidate.ID)
	if err != nil {
		return ChangeImpact{}, err
	}

	out := ChangeImpact{ChangeSetID: cs.ID, ProjectID: cs.ProjectID, Agent: cs.AgentName, BaseVersion: cs.Base.Version,
		CandidateVersion: cs.Candidate.Version, Policy: pol, Problems: []ImpactProblem{}, ComputedAt: a.Now().UTC()}
	var notes []string
	note := func(n string) {
		if n != "" {
			notes = append(notes, n)
		}
	}
	// The services act for the caller, in this project only.
	scoped := p
	scoped.ProjectIDs, scoped.AllProjects = []string{cs.ProjectID}, false

	seeds, n := impact.Seeds(seeds, cand)
	note(n)
	var g *impact.Graph
	if len(seeds) == 0 {
		// Nothing changed that the graph knows of: its answer is empty.
		g = &impact.Graph{}
	} else {
		body := map[string]any{"project_id": cs.ProjectID, "changes": seeds,
			"scope": map[string]string{"agent": cand.Agent, "version": cand.Version}, "max_depth": pol.MaxDepth}
		var resp struct {
			BlastRadius impact.Graph `json:"blast_radius"`
		}
		if prob := a.ask(ctx, a.Graph, "graph-service", scoped, "/api/v1/blast-radius", body, &resp); prob != nil {
			out.Problems = append(out.Problems, *prob)
		} else {
			g = &resp.BlastRadius
		}
	}
	queries, n := impact.Queries(items, base, cand)
	note(n)
	req, n := impact.NewMatchRequest(cs.ProjectID, cs.AgentName, g, pol, queries)
	note(n)
	var m *impact.Match
	var match impact.Match
	if prob := a.ask(ctx, a.Simulation, "simulation-service", scoped, "/api/v1/scenarios/match", req, &match); prob != nil {
		out.Problems = append(out.Problems, *prob)
	} else {
		m = &match
	}

	out.Impact = impact.Merge(items, g, m, queries)
	if g != nil && g.Truncated {
		note("The blast radius stopped at its node bound; components further away were not reached.")
	}
	if m != nil && m.Truncated {
		note("More scenarios were selected than one answer lists; the named ones come first.")
	}
	if g == nil {
		note("Without the dependency graph, scenarios linked to the changed components are missing.")
	}
	if m == nil && g != nil {
		note("Without the scenario library, only the scenarios the graph links are listed, unconfirmed.")
	}
	out.Notes = append(out.Notes, notes...)
	graphCut := g != nil && g.Truncated
	matchCut := m != nil && m.Truncated
	out.Complete = len(out.Problems) == 0 && !graphCut && !matchCut
	return out, nil
}

// impactVersion reads a compared version's manifest.
func (a *App) impactVersion(ctx context.Context, orgID, versionID string) (impact.Version, error) {
	v, err := a.Store.GetAgentVersionByID(ctx, a.Store.Pool, orgID, versionID)
	if err != nil {
		return impact.Version{}, fmt.Errorf("version %s of a change set: %w", versionID, err)
	}
	var m domain.Manifest
	if err := json.Unmarshal(v.Manifest, &m); err != nil {
		return impact.Version{}, fmt.Errorf("stored manifest of %s@%s: %w", v.AgentName, v.Version, err)
	}
	return impact.Version{Agent: m.Name, Version: m.Version, Tools: m.Tools}, nil
}

// ask calls a service; a service that is not configured, not reachable or
// failing is a problem of the impact, not an error of the request.
func (a *App) ask(ctx context.Context, c *svcclient.Client, service string, p authn.Principal, path string, in, out any) *ImpactProblem {
	if c == nil {
		return &ImpactProblem{Service: service, Code: "NOT_CONFIGURED",
			Message: fmt.Sprintf("The %s is not configured on the control plane.", service)}
	}
	err := c.Do(ctx, p, http.MethodPost, path, in, out)
	if err == nil {
		return nil
	}
	prob := &ImpactProblem{Service: service, Code: "UPSTREAM_ERROR", Message: fmt.Sprintf("The %s failed to answer.", service)}
	var he *httpx.Error
	if errors.As(err, &he) {
		prob.Code, prob.Message = he.Code, he.Message
	}
	a.Log.WarnContext(ctx, "change impact: a service did not answer", "service", service, "error", err.Error())
	return prob
}
