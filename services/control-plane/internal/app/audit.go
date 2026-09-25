package app

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

func mustHash(v any) string { return hashx.MustOf(v) }

// ListAudit lists audit entries (settings.read: governance data).
func (a *App) ListAudit(ctx context.Context, p authn.Principal, f store.AuditFilter) ([]store.AuditEntry, error) {
	if err := require(p, authn.PermSettingsRead); err != nil {
		return nil, err
	}
	if f.ProjectID != "" && !p.CanAccessProject(f.ProjectID) {
		return []store.AuditEntry{}, nil
	}
	if !p.AllProjects && f.ProjectID == "" && len(p.ProjectIDs) == 1 {
		f.ProjectID = p.ProjectIDs[0]
	}
	return a.Store.ListAudit(ctx, p.OrgID, f)
}

// VerifyAudit recomputes the organization's audit hash chain.
func (a *App) VerifyAudit(ctx context.Context, p authn.Principal) (store.ChainReport, error) {
	if err := require(p, authn.PermSettingsRead); err != nil {
		return store.ChainReport{}, err
	}
	return a.Store.VerifyAuditChain(ctx, p.OrgID)
}

type auditPayload struct {
	Actor        string         `json:"actor"`
	Action       string         `json:"action"`
	ResourceType string         `json:"resource_type"`
	ResourceID   string         `json:"resource_id"`
	Timestamp    time.Time      `json:"timestamp"`
	RequestID    *string        `json:"request_id"`
	BeforeHash   *string        `json:"before_hash"`
	AfterHash    *string        `json:"after_hash"`
	Reason       *string        `json:"reason"`
	Metadata     map[string]any `json:"metadata"`
}

// HandleAuditEvent persists audit.recorded.v1 events from other services into
// the central, hash-chained log. Duplicate deliveries are ignored via the
// source event id.
func (a *App) HandleAuditEvent(ctx context.Context, env events.Envelope) error {
	var pl auditPayload
	if err := env.Decode(&pl); err != nil {
		return err
	}
	return a.Store.Tx(ctx, func(tx pgx.Tx) error {
		eventID := env.ID
		producer := env.Producer
		if producer == "" {
			producer = "unknown"
		}
		e := store.AuditEntry{
			OrganizationID: env.OrganizationID, ProjectID: env.ProjectID, Actor: pl.Actor, Action: pl.Action,
			ResourceType: pl.ResourceType, ResourceID: pl.ResourceID, OccurredAt: pl.Timestamp, RequestID: pl.RequestID,
			BeforeHash: pl.BeforeHash, AfterHash: pl.AfterHash, Reason: pl.Reason, Metadata: pl.Metadata,
			SourceService: producer, SourceEventID: &eventID,
		}
		if e.Metadata == nil {
			e.Metadata = map[string]any{}
		}
		if _, err := a.Store.AppendAudit(ctx, tx, e); err != nil {
			return fmt.Errorf("append audit: %w", err)
		}
		return nil
	})
}

// compile-time check that audit payloads stay JSON-compatible.
var _ = json.Marshal
