// Package authn defines principals, roles, permissions and the short-lived
// internal service token used between AgentTwin services (ADR-0009).
package authn

import (
	"context"
	"slices"
)

// Role is an organization membership role.
type Role string

const (
	RoleOwner    Role = "OWNER"
	RoleAdmin    Role = "ADMIN"
	RoleEngineer Role = "ENGINEER"
	RoleReviewer Role = "REVIEWER"
	RoleViewer   Role = "VIEWER"
	// RoleAPIKey marks machine principals; their rights come from scopes.
	RoleAPIKey Role = "API_KEY"
	// RoleService marks service-to-service calls originating from events.
	RoleService Role = "SERVICE"
)

// ValidRole reports whether r is a human membership role.
func ValidRole(r Role) bool {
	switch r {
	case RoleOwner, RoleAdmin, RoleEngineer, RoleReviewer, RoleViewer:
		return true
	}
	return false
}

// Permission is an authorization check point.
type Permission string

const (
	PermRead              Permission = "read"
	PermSettingsRead      Permission = "settings.read"
	PermSettingsWrite     Permission = "settings.write"
	PermAPIKeyManage      Permission = "apikey.manage"
	PermAgentWrite        Permission = "agent.write"
	PermScenarioWrite     Permission = "scenario.write"
	PermSimulationRun     Permission = "simulation.run"
	PermEvalRun           Permission = "eval.run"
	PermReleaseWrite      Permission = "release.write"
	PermReleaseOverride   Permission = "release.override"
	PermReviewWrite       Permission = "review.write"
	PermRegressionPromote Permission = "regression.promote"
	PermApprovalDecide    Permission = "approval.decide"
	PermPolicyWrite       Permission = "policy.write"
	PermPolicyActivate    Permission = "policy.activate"
	PermPolicyTest        Permission = "policy.test"
	PermTraceWrite        Permission = "trace.write"
	PermRuntimeInvoke     Permission = "runtime.invoke"
	PermGraphWrite        Permission = "graph.write"
)

// rolePermissions is the explicit RBAC matrix. Keep it a table: reviewers of
// this code must be able to audit who may do what at a glance.
var rolePermissions = map[Role][]Permission{
	RoleOwner: {PermRead, PermSettingsRead, PermSettingsWrite, PermAPIKeyManage, PermAgentWrite, PermScenarioWrite,
		PermSimulationRun, PermEvalRun, PermReleaseWrite, PermReleaseOverride, PermReviewWrite, PermRegressionPromote,
		PermApprovalDecide, PermPolicyWrite, PermPolicyActivate, PermPolicyTest, PermGraphWrite},
	RoleAdmin: {PermRead, PermSettingsRead, PermSettingsWrite, PermAPIKeyManage, PermAgentWrite, PermScenarioWrite,
		PermSimulationRun, PermEvalRun, PermReleaseWrite, PermReleaseOverride, PermReviewWrite, PermRegressionPromote,
		PermApprovalDecide, PermPolicyWrite, PermPolicyActivate, PermPolicyTest, PermGraphWrite},
	RoleEngineer: {PermRead, PermSettingsRead, PermAgentWrite, PermScenarioWrite, PermSimulationRun, PermEvalRun,
		PermReleaseWrite, PermReviewWrite, PermPolicyTest, PermGraphWrite},
	RoleReviewer: {PermRead, PermReviewWrite, PermRegressionPromote, PermApprovalDecide, PermReleaseOverride, PermPolicyTest},
	RoleViewer:   {PermRead},
}

// Scope is an API-key scope.
type Scope string

const (
	ScopeTracesWrite   Scope = "traces:write"
	ScopeRuntimeInvoke Scope = "runtime:invoke"
	ScopeRead          Scope = "read"
	ScopeCI            Scope = "ci"
)

// ValidScope reports whether s is a known API key scope.
func ValidScope(s Scope) bool {
	_, ok := scopePermissions[s]
	return ok
}

var scopePermissions = map[Scope][]Permission{
	ScopeTracesWrite:   {PermTraceWrite},
	ScopeRuntimeInvoke: {PermRuntimeInvoke},
	ScopeRead:          {PermRead},
	ScopeCI:            {PermRead, PermAgentWrite, PermScenarioWrite, PermSimulationRun, PermEvalRun, PermReleaseWrite, PermPolicyTest},
}

// Principal is an authenticated caller.
type Principal struct {
	OrgID       string   `json:"org"`
	Actor       string   `json:"sub"` // user:<id> | apikey:<id> | service:<name>
	Email       string   `json:"email,omitempty"`
	Role        Role     `json:"role"`
	ProjectIDs  []string `json:"projects,omitempty"`
	AllProjects bool     `json:"all_projects,omitempty"`
	Scopes      []Scope  `json:"scopes,omitempty"`
}

// Can reports whether the principal holds perm.
func (p Principal) Can(perm Permission) bool {
	switch p.Role {
	case RoleAPIKey:
		for _, s := range p.Scopes {
			if slices.Contains(scopePermissions[s], perm) {
				return true
			}
		}
		return false
	case RoleService:
		// Service principals act on behalf of an already-authorized flow
		// (e.g. an event from the control plane); they are limited to the
		// organization/projects embedded in the token.
		return true
	default:
		return slices.Contains(rolePermissions[p.Role], perm)
	}
}

// CanAccessProject reports whether the principal may touch projectID.
func (p Principal) CanAccessProject(projectID string) bool {
	if projectID == "" {
		return false
	}
	return p.AllProjects || slices.Contains(p.ProjectIDs, projectID)
}

type principalKey struct{}

// WithPrincipal stores p in ctx.
func WithPrincipal(ctx context.Context, p Principal) context.Context {
	return context.WithValue(ctx, principalKey{}, p)
}

// FromContext returns the principal in ctx.
func FromContext(ctx context.Context) (Principal, bool) {
	p, ok := ctx.Value(principalKey{}).(Principal)
	return p, ok
}

// Matrix returns the role and scope permission tables (for differential tests
// against the Python implementation; see packages/gokit/cmd/parity).
func Matrix() map[string]map[string][]Permission {
	roles := map[string][]Permission{}
	for r, perms := range rolePermissions {
		roles[string(r)] = slices.Sorted(slices.Values(perms))
	}
	scopes := map[string][]Permission{}
	for s, perms := range scopePermissions {
		scopes[string(s)] = slices.Sorted(slices.Values(perms))
	}
	return map[string]map[string][]Permission{"roles": roles, "scopes": scopes}
}
