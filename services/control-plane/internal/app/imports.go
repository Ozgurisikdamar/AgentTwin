package app

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/catalog"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// MaxImportDocumentBytes bounds an OpenAPI document given as text.
const MaxImportDocumentBytes = 2 << 20

// sourceName is a catalog's name: the graph's HTTP API or MCP server key.
var sourceName = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]{0,62}$`)

// ImportOptions are the importer's decisions about names and risk.
type ImportOptions struct {
	RiskOverrides    map[string]domain.RiskLevel `json:"risk_overrides,omitempty"`
	Names            map[string]string           `json:"names,omitempty"`
	TrustAnnotations bool                        `json:"trust_annotations,omitempty"`
}

func (o ImportOptions) catalog() catalog.Options {
	return catalog.Options{RiskOverrides: o.RiskOverrides, Names: o.Names, TrustAnnotations: o.TrustAnnotations}
}

// ImportOpenAPIInput imports an OpenAPI 3 document.
type ImportOpenAPIInput struct {
	// Name identifies the API across imports (and in the graph).
	Name string `json:"name"`
	// Service is the service the API belongs to, when the graph knows it.
	Service string `json:"service,omitempty"`
	// Document is the OpenAPI document: a JSON object, or YAML or JSON text.
	Document      json.RawMessage             `json:"document"`
	RiskOverrides map[string]domain.RiskLevel `json:"risk_overrides,omitempty"`
	Names         map[string]string           `json:"names,omitempty"`
}

// ImportMCPInput imports the tools an MCP server lists.
type ImportMCPInput struct {
	Server          catalog.MCPServer `json:"server"`
	ProtocolVersion string            `json:"protocol_version,omitempty"`
	// Tools are the tools of the server's tools/list result, every page.
	Tools            []any                       `json:"tools"`
	RiskOverrides    map[string]domain.RiskLevel `json:"risk_overrides,omitempty"`
	Names            map[string]string           `json:"names,omitempty"`
	TrustAnnotations bool                        `json:"trust_annotations,omitempty"`
}

// Registry outcomes of a catalog entry.
const (
	RegistryCreated   = "created"
	RegistryUpdated   = "updated"
	RegistryUnchanged = "unchanged"
	// RegistryKeptManifest: an agent manifest declares the tool; a
	// manifest is the stronger source and is not overwritten.
	RegistryKeptManifest = "kept_manifest"
	// RegistryKeptOther: another source (another import, or a person)
	// defines the tool; map the name to import it separately.
	RegistryKeptOther = "kept_other_source"
)

// ImportedEntry is a catalog entry with what it did to the registry.
type ImportedEntry struct {
	catalog.Entry
	Registry     string `json:"registry"`
	ToolVersion  *int   `json:"tool_version,omitempty"`
	RegistryNote string `json:"registry_note,omitempty"`
}

// CatalogSummary counts what an import did.
type CatalogSummary struct {
	Tools     int            `json:"tools"`
	Created   int            `json:"created"`
	Updated   int            `json:"updated"`
	Unchanged int            `json:"unchanged"`
	Kept      int            `json:"kept"`
	Skipped   int            `json:"skipped"`
	Warnings  int            `json:"warnings"`
	Mutating  int            `json:"mutating"`
	Risks     map[string]int `json:"risks"`
}

// ImportedCatalog is a stored catalog; Created is false when the import
// equals the latest revision and nothing was stored.
type ImportedCatalog struct {
	store.Catalog
	Created bool `json:"created"`
}

func invalidImport(msg, field string) error {
	return httpx.Invalid("INVALID_IMPORT", msg, map[string]any{"field": field})
}

func invalidDocument(reason string, details map[string]any) error {
	if details == nil {
		details = map[string]any{}
	}
	details["reason"] = reason
	return httpx.Invalid("INVALID_DOCUMENT", "The document cannot be imported: "+reason, details)
}

// ImportOpenAPI imports an OpenAPI document into the project's tool
// registry (spec §20.2). Requires agent.write.
func (a *App) ImportOpenAPI(ctx context.Context, p authn.Principal, projectID string, in ImportOpenAPIInput) (ImportedCatalog, error) {
	if _, err := a.projectFor(ctx, p, authn.PermAgentWrite, projectID); err != nil {
		return ImportedCatalog{}, err
	}
	in.Name, in.Service = strings.TrimSpace(in.Name), strings.TrimSpace(in.Service)
	if !sourceName.MatchString(in.Name) {
		return ImportedCatalog{}, invalidImport("name identifies the API: lowercase letters, digits, '.', '-' or '_' (max 63).", "name")
	}
	if in.Service != "" && !sourceName.MatchString(in.Service) {
		return ImportedCatalog{}, invalidImport("service is a service name: lowercase letters, digits, '.', '-' or '_' (max 63).", "service")
	}
	doc, err := decodeImportDocument(in.Document)
	if err != nil {
		return ImportedCatalog{}, err
	}
	opts := ImportOptions{RiskOverrides: in.RiskOverrides, Names: in.Names}
	cat, err := catalog.FromOpenAPI(doc, opts.catalog())
	if err != nil {
		return ImportedCatalog{}, catalogError(err)
	}
	var service *string
	if in.Service != "" {
		service = &in.Service
	}
	return a.importCatalog(ctx, p, projectID, in.Name, service, cat, opts, hashx.MustOf(doc))
}

// ImportMCP imports the tools an MCP server lists (spec §20.3, §33). The
// server is not contacted: its tools/list result is the input.
func (a *App) ImportMCP(ctx context.Context, p authn.Principal, projectID string, in ImportMCPInput) (ImportedCatalog, error) {
	if _, err := a.projectFor(ctx, p, authn.PermAgentWrite, projectID); err != nil {
		return ImportedCatalog{}, err
	}
	in.Server.Name = strings.TrimSpace(in.Server.Name)
	if !sourceName.MatchString(in.Server.Name) {
		return ImportedCatalog{}, invalidImport("server.name identifies the server: lowercase letters, digits, '.', '-' or '_' (max 63).", "server.name")
	}
	if u := strings.TrimSpace(in.Server.URL); u != "" {
		parsed, err := url.Parse(u)
		if err != nil || (parsed.Scheme != "https" && parsed.Scheme != "http") || parsed.Host == "" || len(u) > 500 {
			return ImportedCatalog{}, invalidImport("server.url is the server's http(s) URL (it is recorded, not contacted).", "server.url")
		}
	}
	if len(in.Server.Version) > 100 || len(in.ProtocolVersion) > 20 {
		return ImportedCatalog{}, invalidImport("server.version is at most 100 characters, protocol_version at most 20.", "server.version")
	}
	if in.Tools == nil {
		return ImportedCatalog{}, invalidImport("tools lists the server's tools (the tools of its tools/list result).", "tools")
	}
	opts := ImportOptions{RiskOverrides: in.RiskOverrides, Names: in.Names, TrustAnnotations: in.TrustAnnotations}
	cat, err := catalog.FromMCP(in.Server, in.ProtocolVersion, in.Tools, opts.catalog())
	if err != nil {
		return ImportedCatalog{}, catalogError(err)
	}
	return a.importCatalog(ctx, p, projectID, in.Server.Name, nil, cat, opts,
		hashx.MustOf(map[string]any{"server": in.Server, "protocol_version": in.ProtocolVersion, "tools": in.Tools}))
}

func catalogError(err error) error {
	var ce *catalog.Error
	if errors.As(err, &ce) {
		return invalidDocument(ce.Reason, nil)
	}
	return err
}

// decodeImportDocument accepts a JSON object or YAML/JSON text.
func decodeImportDocument(raw json.RawMessage) (map[string]any, error) {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || string(raw) == "null" {
		return nil, invalidImport("document is the OpenAPI document (an object, or YAML or JSON text).", "document")
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		doc, err := domain.DecodeDocumentLimit([]byte(text), MaxImportDocumentBytes)
		if err != nil {
			// DecodeDocument speaks of manifests; say what failed.
			var me *domain.ManifestError
			if errors.As(err, &me) && len(me.Problems) > 0 {
				return nil, invalidDocument(me.Problems[0].Message, nil)
			}
			return nil, invalidDocument("unreadable", nil)
		}
		return doc, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var doc map[string]any
	if err := dec.Decode(&doc); err != nil || doc == nil {
		return nil, invalidImport("document is the OpenAPI document (an object, or YAML or JSON text).", "document")
	}
	return doc, nil
}

func (a *App) importCatalog(ctx context.Context, p authn.Principal, projectID, name string, service *string,
	cat catalog.Catalog, opts ImportOptions, documentSHA string) (ImportedCatalog, error) {
	if len(cat.Entries) == 0 {
		return ImportedCatalog{}, invalidDocument("no operation became a tool", map[string]any{"skipped": cat.Skipped, "warnings": cat.Warnings})
	}
	if opts.RiskOverrides == nil {
		opts.RiskOverrides = map[string]domain.RiskLevel{}
	}
	if opts.Names == nil {
		opts.Names = map[string]string{}
	}
	content := hashx.MustOf(map[string]any{"catalog": cat, "service": service, "options": opts})
	ref := strings.ToLower(cat.Source) + ":" + name
	var id string
	created := false
	err := a.Store.Tx(ctx, func(tx pgx.Tx) error {
		if err := a.Store.LockCatalogSource(ctx, tx, p.OrgID, projectID, cat.Source, name); err != nil {
			return err
		}
		revision := 1
		latest, err := a.Store.LatestCatalog(ctx, tx, p.OrgID, projectID, cat.Source, name)
		switch {
		case err == nil && latest.ContentSHA256 == content:
			// The same import again: nothing changes.
			id = latest.ID
			return nil
		case err == nil:
			revision = latest.Revision + 1
		case !errors.Is(err, store.ErrNotFound):
			return err
		}
		entries := make([]ImportedEntry, 0, len(cat.Entries))
		sum := CatalogSummary{Tools: len(cat.Entries), Skipped: len(cat.Skipped), Warnings: len(cat.Warnings), Risks: map[string]int{}}
		type eventTool struct {
			Name     string  `json:"name"`
			Risk     string  `json:"risk"`
			Method   *string `json:"method"`
			Path     *string `json:"path"`
			Mutating bool    `json:"mutating"`
		}
		tools := make([]eventTool, 0, len(cat.Entries))
		for _, e := range cat.Entries {
			ie, err := a.registerEntry(ctx, tx, p, projectID, cat.Source, ref, e)
			if err != nil {
				return err
			}
			entries = append(entries, ie)
			switch ie.Registry {
			case RegistryCreated:
				sum.Created++
			case RegistryUpdated:
				sum.Updated++
			case RegistryUnchanged:
				sum.Unchanged++
			default:
				sum.Kept++
			}
			if e.Mutating {
				sum.Mutating++
			}
			sum.Risks[string(e.Risk)]++
			t := eventTool{Name: e.Name, Risk: string(e.Risk), Mutating: e.Mutating}
			if e.Method != "" {
				t.Method, t.Path = &e.Method, &e.Path
			}
			tools = append(tools, t)
		}
		id = newID()
		if err := a.Store.InsertCatalog(ctx, tx, store.NewCatalog{
			ID: id, OrganizationID: p.OrgID, ProjectID: projectID, Source: cat.Source, Name: name, Revision: revision,
			Service: service, Title: cat.Title, APIVersion: cat.APIVersion, SpecVersion: cat.SpecVersion,
			Servers: cat.Servers, Options: opts, Entries: entries, Skipped: cat.Skipped, Warnings: cat.Warnings, Summary: sum,
			ContentSHA256: content, DocumentSHA256: documentSHA, CreatedBy: p.Actor,
		}); err != nil {
			return err
		}
		created = true
		// The graph links every tool to the API or server, also the tools
		// the registry kept from another source: the call is a fact.
		if err := a.emit(ctx, tx, "tool.catalog_imported.v1", p.OrgID, projectID, map[string]any{
			"source": cat.Source, "source_name": name, "service": service, "catalog_id": id, "revision": revision, "tools": tools,
		}); err != nil {
			return err
		}
		return a.audit(ctx, tx, p, projectID, "tool_catalog.imported", "tool_catalog", id, nil,
			map[string]any{"content_sha256": content}, "",
			map[string]any{"source": cat.Source, "name": name, "revision": revision, "tools": sum.Tools, "created": sum.Created, "updated": sum.Updated})
	})
	if errors.Is(err, store.ErrConflict) {
		// A manifest registered one of the tools at the same moment.
		return ImportedCatalog{}, httpx.NewError(409, "CONCURRENT_CHANGE", "The tool registry changed during the import; retry it.")
	}
	if err != nil {
		return ImportedCatalog{}, err
	}
	c, err := a.Store.GetCatalog(ctx, a.Store.Pool, p.OrgID, id)
	if err != nil {
		return ImportedCatalog{}, err
	}
	return ImportedCatalog{Catalog: c, Created: created}, nil
}

// registerEntry writes an entry to the tool registry unless a stronger or
// another source defines the tool.
func (a *App) registerEntry(ctx context.Context, tx pgx.Tx, p authn.Principal, projectID, source, ref string, e catalog.Entry) (ImportedEntry, error) {
	ie := ImportedEntry{Entry: e}
	own, err := a.Store.GetToolOwnership(ctx, tx, p.OrgID, projectID, e.Name)
	if err != nil {
		return ie, err
	}
	switch {
	case own.Manifest:
		ie.Registry, ie.RegistryNote = RegistryKeptManifest, "an agent manifest declares this tool"
		return ie, nil
	case own.Exists && (own.LatestSource != source || own.LatestRef != ref):
		ie.Registry = RegistryKeptOther
		ie.RegistryNote = fmt.Sprintf("defined by %s", strings.ToLower(own.LatestSource))
		if own.LatestRef != "" {
			ie.RegistryNote += " " + own.LatestRef
		}
		return ie, nil
	}
	def := store.ToolDefinition{Name: e.Name, Description: e.Description, Risk: string(e.Risk), InputSchema: e.InputSchema,
		Source: source, SourceRef: ref}
	def.DefinitionSHA256 = mustHash(map[string]any{"risk": def.Risk, "input_schema": def.InputSchema, "description": def.Description,
		"source": def.Source, "source_ref": def.SourceRef})
	_, _, version, created, err := a.Store.UpsertToolVersion(ctx, tx, p.OrgID, projectID, def, p.Actor)
	if err != nil {
		return ie, err
	}
	ie.ToolVersion = &version
	switch {
	case !created:
		ie.Registry = RegistryUnchanged
	case version == 1:
		ie.Registry = RegistryCreated
	default:
		ie.Registry = RegistryUpdated
	}
	return ie, nil
}

// GetCatalog returns a catalog of a project the caller may read.
func (a *App) GetCatalog(ctx context.Context, p authn.Principal, id string) (store.Catalog, error) {
	if err := require(p, authn.PermRead); err != nil {
		return store.Catalog{}, err
	}
	c, err := a.Store.GetCatalog(ctx, a.Store.Pool, p.OrgID, id)
	if err != nil {
		return store.Catalog{}, notFoundOr(err)
	}
	if !p.CanAccessProject(c.ProjectID) {
		return store.Catalog{}, httpx.ErrNotFound
	}
	return c, nil
}

// CatalogPage selects a page of a project's catalogs.
type CatalogPage struct {
	Source, Name string
	Before       *httpx.Cursor
	Limit        int
}

// ListCatalogs lists a project's catalogs, newest first.
func (a *App) ListCatalogs(ctx context.Context, p authn.Principal, projectID string, page CatalogPage) ([]store.CatalogSummary, error) {
	if _, err := a.projectFor(ctx, p, authn.PermRead, projectID); err != nil {
		return nil, err
	}
	switch page.Source {
	case "", catalog.SourceOpenAPI, catalog.SourceMCP:
	default:
		return nil, httpx.Invalid("INVALID_PARAMETER", "source is OPENAPI or MCP.", map[string]any{"field": "source"})
	}
	if page.Name != "" && !sourceName.MatchString(page.Name) {
		return nil, httpx.Invalid("INVALID_PARAMETER", "name is a catalog name.", map[string]any{"field": "name"})
	}
	f := store.CatalogFilter{ProjectID: projectID, Source: page.Source, Name: page.Name, Limit: page.Limit}
	if page.Before != nil {
		t := page.Before.TS.UTC().Truncate(time.Microsecond)
		f.Before, f.BeforeID = &t, page.Before.ID
	}
	return a.Store.ListCatalogs(ctx, p.OrgID, f)
}
