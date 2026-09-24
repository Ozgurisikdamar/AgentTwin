package store

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
)

// AuditEntry is one governance event.
type AuditEntry struct {
	ID             string         `json:"id"`
	OrganizationID string         `json:"organization_id"`
	ProjectID      *string        `json:"project_id,omitempty"`
	Actor          string         `json:"actor"`
	Action         string         `json:"action"`
	ResourceType   string         `json:"resource_type"`
	ResourceID     string         `json:"resource_id"`
	OccurredAt     time.Time      `json:"occurred_at"`
	RequestID      *string        `json:"request_id,omitempty"`
	BeforeHash     *string        `json:"before_hash,omitempty"`
	AfterHash      *string        `json:"after_hash,omitempty"`
	Reason         *string        `json:"reason,omitempty"`
	Metadata       map[string]any `json:"metadata"`
	SourceService  string         `json:"source_service"`
	SourceEventID  *string        `json:"source_event_id,omitempty"`
	PrevHash       *string        `json:"prev_hash,omitempty"`
	EntryHash      string         `json:"entry_hash"`
	Seq            int64          `json:"seq"`
}

func strOrNil(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func deref(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// entryDigest is the hashed view of an entry; order-independent via canonical JSON.
func entryDigest(e AuditEntry, prev string) string {
	body := hashx.MustOf(map[string]any{
		"id": e.ID, "organization_id": e.OrganizationID, "project_id": deref(e.ProjectID),
		"actor": e.Actor, "action": e.Action, "resource_type": e.ResourceType, "resource_id": e.ResourceID,
		"occurred_at": e.OccurredAt.UTC().Format(time.RFC3339Nano), "request_id": deref(e.RequestID),
		"before_hash": deref(e.BeforeHash), "after_hash": deref(e.AfterHash), "reason": deref(e.Reason),
		"metadata": e.Metadata, "source_service": e.SourceService, "source_event_id": deref(e.SourceEventID),
	})
	return hashx.SHA256Hex([]byte(prev + "|" + body))
}

// AppendAudit appends an entry to the organization's hash chain inside tx.
// A per-organization advisory lock serializes appends so the chain cannot fork.
// Returns false when source_event_id was already recorded (idempotent consume).
func (s *Store) AppendAudit(ctx context.Context, tx pgx.Tx, e AuditEntry) (bool, error) {
	if e.ID == "" {
		e.ID = ids.New()
	}
	if e.OccurredAt.IsZero() {
		e.OccurredAt = s.Now()
	}
	// PostgreSQL stores microseconds; hash what is stored.
	e.OccurredAt = e.OccurredAt.UTC().Truncate(time.Microsecond)
	if e.Metadata == nil {
		e.Metadata = map[string]any{}
	}
	if e.SourceService == "" {
		e.SourceService = "control-plane"
	}
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('audit:' || $1::text))`, e.OrganizationID); err != nil {
		return false, err
	}
	if e.SourceEventID != nil {
		var exists bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM control.audit_event WHERE source_event_id = $1)`, *e.SourceEventID).Scan(&exists); err != nil {
			return false, err
		}
		if exists {
			return false, nil
		}
	}
	var prev *string
	err := tx.QueryRow(ctx, `SELECT entry_hash FROM control.audit_event WHERE organization_id = $1 ORDER BY seq DESC LIMIT 1`, e.OrganizationID).Scan(&prev)
	if err != nil && !isNoRows(err) {
		return false, err
	}
	e.PrevHash = prev
	e.EntryHash = entryDigest(e, deref(prev))
	meta, err := json.Marshal(e.Metadata)
	if err != nil {
		return false, err
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO control.audit_event (id, organization_id, project_id, actor, action, resource_type, resource_id, occurred_at,
			request_id, before_hash, after_hash, reason, metadata, source_service, source_event_id, prev_hash, entry_hash)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)`,
		e.ID, e.OrganizationID, e.ProjectID, e.Actor, e.Action, e.ResourceType, e.ResourceID, e.OccurredAt, e.RequestID,
		e.BeforeHash, e.AfterHash, e.Reason, meta, e.SourceService, e.SourceEventID, e.PrevHash, e.EntryHash)
	if err != nil {
		return false, mapErr(err)
	}
	return true, nil
}

func isNoRows(err error) bool { return errors.Is(mapErr(err), ErrNotFound) }

const auditCols = `id, organization_id, project_id, actor, action, resource_type, resource_id, occurred_at, request_id,
	before_hash, after_hash, reason, metadata, source_service, source_event_id, prev_hash, entry_hash, seq`

func scanAudit(r pgx.Row) (AuditEntry, error) {
	var e AuditEntry
	var meta []byte
	err := r.Scan(&e.ID, &e.OrganizationID, &e.ProjectID, &e.Actor, &e.Action, &e.ResourceType, &e.ResourceID, &e.OccurredAt,
		&e.RequestID, &e.BeforeHash, &e.AfterHash, &e.Reason, &meta, &e.SourceService, &e.SourceEventID, &e.PrevHash, &e.EntryHash, &e.Seq)
	if err == nil {
		e.Metadata = map[string]any{}
		if len(meta) > 0 {
			dec := json.NewDecoder(bytes.NewReader(meta))
			dec.UseNumber()
			_ = dec.Decode(&e.Metadata)
		}
	}
	return e, mapErr(err)
}

// AuditFilter filters audit listings.
type AuditFilter struct {
	ProjectID    string
	ResourceType string
	ResourceID   string
	Action       string
	BeforeSeq    int64
	Limit        int
}

// ListAudit lists audit entries newest first with seq-based pagination.
func (s *Store) ListAudit(ctx context.Context, orgID string, f AuditFilter) ([]AuditEntry, error) {
	if f.Limit <= 0 || f.Limit > 200 {
		f.Limit = 50
	}
	rows, err := s.Pool.Query(ctx, `SELECT `+auditCols+` FROM control.audit_event
		WHERE organization_id = $1
		  AND ($2 = '' OR project_id::text = $2)
		  AND ($3 = '' OR resource_type = $3)
		  AND ($4 = '' OR resource_id = $4)
		  AND ($5 = '' OR action = $5)
		  AND ($6 = 0 OR seq < $6)
		ORDER BY seq DESC LIMIT $7`, orgID, f.ProjectID, f.ResourceType, f.ResourceID, f.Action, f.BeforeSeq, f.Limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(r pgx.CollectableRow) (AuditEntry, error) { return scanAudit(r) })
}

// ChainReport summarizes a hash-chain verification.
type ChainReport struct {
	Entries  int    `json:"entries"`
	Valid    bool   `json:"valid"`
	BrokenAt *int64 `json:"broken_at_seq,omitempty"`
	Reason   string `json:"reason,omitempty"`
}

// VerifyAuditChain recomputes every entry hash of the organization in order.
func (s *Store) VerifyAuditChain(ctx context.Context, orgID string) (ChainReport, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+auditCols+` FROM control.audit_event WHERE organization_id = $1 ORDER BY seq`, orgID)
	if err != nil {
		return ChainReport{}, err
	}
	entries, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (AuditEntry, error) { return scanAudit(r) })
	if err != nil {
		return ChainReport{}, err
	}
	rep := ChainReport{Entries: len(entries), Valid: true}
	prev := ""
	for _, e := range entries {
		if deref(e.PrevHash) != prev {
			seq := e.Seq
			return ChainReport{Entries: len(entries), Valid: false, BrokenAt: &seq, Reason: "prev_hash does not match previous entry"}, nil
		}
		if got := entryDigest(e, prev); got != e.EntryHash {
			seq := e.Seq
			return ChainReport{Entries: len(entries), Valid: false, BrokenAt: &seq, Reason: fmt.Sprintf("entry hash mismatch (recomputed %s…)", got[:12])}, nil
		}
		prev = e.EntryHash
	}
	return rep, nil
}

// AuditFromRequest builds an entry with request correlation.
func AuditFromRequest(ctx context.Context, orgID, projectID, actor, action, resourceType, resourceID string) AuditEntry {
	return AuditEntry{
		OrganizationID: orgID, ProjectID: strOrNil(projectID), Actor: actor, Action: action,
		ResourceType: resourceType, ResourceID: resourceID, RequestID: strOrNil(requestIDFrom(ctx)),
	}
}

func requestIDFrom(ctx context.Context) string { return logx.RequestID(ctx) }
