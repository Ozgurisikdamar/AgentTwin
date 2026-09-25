package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/changes"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// changeSetAlgorithm names how items are computed; it is part of a change
// set's content hash, so a different algorithm is a different change set.
const changeSetAlgorithm = "changes-v1"

// Bounds on a change set request.
const (
	maxChangeSetTitle   = 200
	maxDeclaredChanges  = 50
	maxDeclaredName     = 200
	maxDeclaredSummary  = 300
	maxChangedFileBytes = 512
	maxVersionLength    = 100
)

// CreateChangeSetInput names the two versions of one agent to compare.
type CreateChangeSetInput struct {
	Agent            string             `json:"agent"`
	BaseVersion      string             `json:"base_version"`
	CandidateVersion string             `json:"candidate_version"`
	Title            string             `json:"title,omitempty"`
	Git              *changes.Git       `json:"git,omitempty"`
	Declared         []changes.Declared `json:"declared,omitempty"`
}

// CreatedChangeSet is a stored change set; Created is false when the same
// change set was stored before.
type CreatedChangeSet struct {
	store.ChangeSet
	Created bool `json:"created"`
}

// ChangeSetSummary counts a change set's items.
type ChangeSetSummary struct {
	Items      int            `json:"items"`
	Breaking   int            `json:"breaking"`
	Seeds      int            `json:"seeds"`
	Kinds      map[string]int `json:"kinds"`
	Confidence map[string]int `json:"confidence"`
}

func invalidChangeSet(msg, field string) error {
	return httpx.Invalid("INVALID_CHANGE_SET", msg, map[string]any{"field": field})
}

// normalizeChangeSetInput validates the request and puts it in the form
// that is hashed, so equal requests hash equally.
func normalizeChangeSetInput(in *CreateChangeSetInput) error {
	in.Agent = strings.TrimSpace(in.Agent)
	in.BaseVersion = strings.TrimSpace(in.BaseVersion)
	in.CandidateVersion = strings.TrimSpace(in.CandidateVersion)
	in.Title = strings.TrimSpace(in.Title)
	if !agentName.MatchString(in.Agent) {
		return invalidChangeSet("agent must name an agent of the project.", "agent")
	}
	for _, f := range [][2]string{{"base_version", in.BaseVersion}, {"candidate_version", in.CandidateVersion}} {
		if f[1] == "" || len(f[1]) > maxVersionLength {
			return invalidChangeSet(f[0]+" is required (at most 100 characters).", f[0])
		}
	}
	if in.BaseVersion == in.CandidateVersion {
		return invalidChangeSet("base_version and candidate_version must differ.", "candidate_version")
	}
	if utf8.RuneCountInString(in.Title) > maxChangeSetTitle || hasControl(in.Title) {
		return invalidChangeSet("title is at most 200 characters, without control characters.", "title")
	}
	if g := in.Git; g != nil {
		g.BaseCommit, g.CandidateCommit = strings.TrimSpace(g.BaseCommit), strings.TrimSpace(g.CandidateCommit)
		for _, f := range [][2]string{{"git.base_commit", g.BaseCommit}, {"git.candidate_commit", g.CandidateCommit}} {
			if f[1] != "" && !commitPattern.MatchString(f[1]) {
				return invalidChangeSet(f[0]+" must be a lowercase hex git sha (7-64 chars).", f[0])
			}
		}
		if len(g.ChangedFiles) > changes.MaxChangedFiles {
			return invalidChangeSet(fmt.Sprintf("git.changed_files lists at most %d files.", changes.MaxChangedFiles), "git.changed_files")
		}
		var files []string
		for _, f := range g.ChangedFiles {
			f = strings.TrimSpace(f)
			if f == "" || len(f) > maxChangedFileBytes || hasControl(f) {
				return invalidChangeSet("A changed file is a path of 1-512 bytes without control characters.", "git.changed_files")
			}
			if !slices.Contains(files, f) {
				files = append(files, f)
			}
		}
		g.ChangedFiles = files
		if g.BaseCommit == "" && g.CandidateCommit == "" && len(g.ChangedFiles) == 0 {
			in.Git = nil
		}
	}
	if len(in.Declared) > maxDeclaredChanges {
		return invalidChangeSet(fmt.Sprintf("declared lists at most %d changes.", maxDeclaredChanges), "declared")
	}
	seen := map[string]bool{}
	for i := range in.Declared {
		d := &in.Declared[i]
		d.Name, d.Summary = strings.TrimSpace(d.Name), strings.TrimSpace(d.Summary)
		field := fmt.Sprintf("declared[%d]", i)
		switch {
		case d.Kind != "policy" && d.Kind != "evaluator" && d.Kind != "dataset":
			return invalidChangeSet("A declared change is a policy, an evaluator or a dataset.", field+".kind")
		case d.Change != "added" && d.Change != "removed" && d.Change != "modified":
			return invalidChangeSet("A declared change is added, removed or modified.", field+".change")
		case d.Name == "" || utf8.RuneCountInString(d.Name) > maxDeclaredName || hasControl(d.Name):
			return invalidChangeSet("A declared change names what changed (1-200 characters).", field+".name")
		case utf8.RuneCountInString(d.Summary) > maxDeclaredSummary || hasControl(d.Summary):
			return invalidChangeSet("A declared change's summary is at most 300 characters.", field+".summary")
		case seen[d.Kind+"\x00"+d.Name]:
			return invalidChangeSet("The same policy, evaluator or dataset is declared twice.", field+".name")
		}
		seen[d.Kind+"\x00"+d.Name] = true
	}
	slices.SortStableFunc(in.Declared, func(a, b changes.Declared) int {
		return strings.Compare(a.Kind+"\x00"+a.Name, b.Kind+"\x00"+b.Name)
	})
	if in.Declared == nil {
		in.Declared = []changes.Declared{}
	}
	return nil
}

func hasControl(s string) bool {
	return strings.ContainsFunc(s, unicode.IsControl)
}

func changeSetVersion(v store.AgentVersion) (changes.Version, error) {
	var m domain.Manifest
	if err := json.Unmarshal(v.Manifest, &m); err != nil {
		return changes.Version{}, fmt.Errorf("stored manifest of %s@%s: %w", v.AgentName, v.Version, err)
	}
	return changes.Version{Manifest: m, PromptText: v.PromptText, CommitSHA: v.CommitSHA}, nil
}

func summarize(set changes.Set) ChangeSetSummary {
	s := ChangeSetSummary{Items: len(set.Items), Seeds: len(set.Seeds), Kinds: map[string]int{}, Confidence: map[string]int{}}
	for _, it := range set.Items {
		if it.Breaking {
			s.Breaking++
		}
		s.Kinds[it.Kind]++
		s.Confidence[it.Confidence]++
	}
	return s
}

// CreateChangeSet computes and stores what changed between two registered
// versions of an agent (spec §21). A request equal to an earlier or a
// concurrent one (same versions, title, git and declared changes) returns
// the stored change set: one insert decides, so the two cases are one path.
// Requires release.write, which CI keys hold.
func (a *App) CreateChangeSet(ctx context.Context, p authn.Principal, projectID string, in CreateChangeSetInput) (CreatedChangeSet, error) {
	if _, err := a.projectFor(ctx, p, authn.PermReleaseWrite, projectID); err != nil {
		return CreatedChangeSet{}, err
	}
	if err := normalizeChangeSetInput(&in); err != nil {
		return CreatedChangeSet{}, err
	}
	pool := a.Store.Pool
	agent, err := a.Store.GetAgentByName(ctx, p.OrgID, projectID, in.Agent)
	if err != nil {
		return CreatedChangeSet{}, notFoundOr(err)
	}
	base, err := a.Store.GetAgentVersion(ctx, pool, p.OrgID, agent.ID, in.BaseVersion)
	if err != nil {
		return CreatedChangeSet{}, notFoundOr(err)
	}
	cand, err := a.Store.GetAgentVersion(ctx, pool, p.OrgID, agent.ID, in.CandidateVersion)
	if err != nil {
		return CreatedChangeSet{}, notFoundOr(err)
	}
	sha := mustHash(map[string]any{
		"algorithm": changeSetAlgorithm, "base_version_id": base.ID, "candidate_version_id": cand.ID,
		"title": in.Title, "git": in.Git, "declared": in.Declared,
	})
	bv, err := changeSetVersion(base)
	if err != nil {
		return CreatedChangeSet{}, err
	}
	cv, err := changeSetVersion(cand)
	if err != nil {
		return CreatedChangeSet{}, err
	}
	set := changes.Compute(bv, cv, in.Git, in.Declared)
	summary := summarize(set)
	id := newID()
	created := false
	err = a.Store.Tx(ctx, func(tx pgx.Tx) error {
		nc := store.NewChangeSet{
			ID: id, OrganizationID: p.OrgID, ProjectID: projectID, AgentID: agent.ID,
			BaseVersionID: base.ID, CandidateVersionID: cand.ID, Title: in.Title,
			Declared: in.Declared, Items: set.Items, Seeds: set.Seeds, Scope: set.Scope, Summary: summary,
			ContentSHA256: sha, CreatedBy: p.Actor,
		}
		if in.Git != nil {
			nc.Git = in.Git
		}
		inserted, err := a.Store.InsertChangeSet(ctx, tx, nc)
		if err != nil || !inserted {
			// Not inserted: the same change set was stored before, or by a
			// concurrent request; it is returned as it is.
			return err
		}
		created = true
		return a.audit(ctx, tx, p, projectID, "change_set.created", "change_set", id, nil,
			map[string]any{"content_sha256": sha, "items": set.Items}, "",
			map[string]any{"agent": agent.Name, "base_version": base.Version, "candidate_version": cand.Version, "items": summary.Items})
	})
	if err != nil {
		return CreatedChangeSet{}, err
	}
	if !created {
		if id, err = a.Store.ChangeSetIDByContent(ctx, pool, p.OrgID, agent.ID, sha); err != nil {
			return CreatedChangeSet{}, err
		}
	}
	cs, err := a.Store.GetChangeSet(ctx, pool, p.OrgID, id)
	if err != nil {
		return CreatedChangeSet{}, err
	}
	a.changeSetVisibility(p, &cs)
	return CreatedChangeSet{ChangeSet: cs, Created: created}, nil
}

// promptTextHidden replaces a prompt diff for callers who may not read
// prompt text (as GetVersion hides the text itself).
const promptTextHidden = "hidden: reading prompt text requires settings.read"

func (a *App) changeSetVisibility(p authn.Principal, cs *store.ChangeSet) {
	if p.Can(authn.PermSettingsRead) {
		return
	}
	var items []map[string]any
	if json.Unmarshal(cs.Items, &items) != nil {
		cs.Items = json.RawMessage(`[]`)
		return
	}
	for _, it := range items {
		if it["kind"] != "prompt" {
			continue
		}
		if d, ok := it["detail"].(map[string]any); ok {
			if _, has := d["diff"]; has {
				delete(d, "diff")
				delete(d, "diff_truncated")
				d["diff_unavailable"] = promptTextHidden
			}
		}
	}
	cs.Items = jsonRaw(items)
}

// GetChangeSet returns a change set of a project the caller may read.
func (a *App) GetChangeSet(ctx context.Context, p authn.Principal, id string) (store.ChangeSet, error) {
	if err := require(p, authn.PermRead); err != nil {
		return store.ChangeSet{}, err
	}
	cs, err := a.Store.GetChangeSet(ctx, a.Store.Pool, p.OrgID, id)
	if err != nil {
		return store.ChangeSet{}, notFoundOr(err)
	}
	if !p.CanAccessProject(cs.ProjectID) {
		return store.ChangeSet{}, httpx.ErrNotFound
	}
	a.changeSetVisibility(p, &cs)
	return cs, nil
}

// ChangeSetPage selects a page of a project's change sets.
type ChangeSetPage struct {
	Agent  string // optional agent name
	Before *httpx.Cursor
	Limit  int
}

// ListChangeSets lists a project's change sets, newest first. An agent
// filter naming no agent of the project matches nothing.
func (a *App) ListChangeSets(ctx context.Context, p authn.Principal, projectID string, page ChangeSetPage) ([]store.ChangeSetSummary, error) {
	if _, err := a.projectFor(ctx, p, authn.PermRead, projectID); err != nil {
		return nil, err
	}
	f := store.ChangeSetFilter{ProjectID: projectID, Limit: page.Limit}
	if page.Agent != "" {
		if !agentName.MatchString(page.Agent) {
			return nil, httpx.Invalid("INVALID_PARAMETER", "agent must be an agent name.", map[string]any{"field": "agent"})
		}
		ag, err := a.Store.GetAgentByName(ctx, p.OrgID, projectID, page.Agent)
		if errors.Is(err, store.ErrNotFound) {
			return []store.ChangeSetSummary{}, nil
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
	return a.Store.ListChangeSets(ctx, p.OrgID, f)
}
