package catalog

import (
	"regexp"
	"strconv"

	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/domain"
)

// MCPProtocolVersion is the MCP revision the importer is written and
// tested against (spec §33: the current stable one at implementation).
const MCPProtocolVersion = "2026-07-28"

// mcpToolName is a tool name as the MCP specification allows it.
var mcpToolName = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)

// MCPServer names the server whose tools/list result is imported. Its URL
// is recorded, never contacted.
type MCPServer struct {
	Name    string `json:"name"`
	URL     string `json:"url,omitempty"`
	Version string `json:"version,omitempty"`
}

// hintKeys are the tool annotations the importer reads.
var hintKeys = []string{"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}

// FromMCP reads the tools of an MCP tools/list result (every page, in
// order). The server's annotations are claims: they set risk only when
// the importer trusts them, and are recorded either way.
func FromMCP(server MCPServer, protocolVersion string, tools []any, opts Options) (Catalog, error) {
	if err := opts.Validate(); err != nil {
		return Catalog{}, err
	}
	if len(tools) > MaxEntries*2 {
		return Catalog{}, fail("the server lists more than %d tools", MaxEntries*2)
	}
	b := newBuilder(SourceMCP, opts)
	b.c.Title = cleanText(server.Name, 200)
	b.c.APIVersion = cleanText(server.Version, 100)
	b.c.SpecVersion = cleanText(protocolVersion, 20)
	if u := serverURL(server.URL); u != "" {
		b.c.Servers = append(b.c.Servers, u)
	}
	if protocolVersion != "" && protocolVersion != MCPProtocolVersion {
		b.warn("the server speaks MCP %s; the importer reads %s", b.c.SpecVersion, MCPProtocolVersion)
	}
	seen := map[string]bool{}
	for i, raw := range tools {
		t := asMap(raw)
		mcpName := str(t["name"])
		op := mcpName
		switch {
		case t == nil:
			b.skip("#"+strconv.Itoa(i), "not a tool object")
			continue
		case !mcpToolName.MatchString(mcpName):
			b.skip(cleanText(nonEmpty(mcpName, "#"+strconv.Itoa(i)), 130), "not an MCP tool name (1-128 of A-Z a-z 0-9 _ - .)")
			continue
		case seen[mcpName]:
			b.skip(op, "listed twice")
			continue
		}
		seen[mcpName] = true
		schema := asMap(t["inputSchema"])
		if schema == nil || schema["type"] != "object" {
			b.skip(op, "its inputSchema is not an object schema")
			continue
		}
		name, ok := b.name(op, []string{mcpName}, "", mcpName)
		if !ok {
			continue
		}
		e := Entry{Name: name, Operation: op, Title: cleanText(nonEmpty(str(t["title"]), str(asMap(t["annotations"])["title"])), 200),
			Description: cleanText(str(t["description"]), maxDescription)}
		r := &resolver{doc: schema, b: b}
		e.InputSchema, _ = r.schema(op, schema).(map[string]any)
		if e.InputSchema == nil {
			e.InputSchema = map[string]any{"type": "object"}
		}
		e.InputSchema = b.boundSchema(op, e.InputSchema)
		e.Hints, e.HintRisk = hints(asMap(t["annotations"]))
		if risk, ok := b.override(name, mcpName); ok {
			e.Risk, e.RiskSource = risk, RiskOverride
		} else if opts.TrustAnnotations && e.Hints != nil {
			e.Risk, e.RiskSource = e.HintRisk, RiskAnnotation
		} else {
			e.Risk, e.RiskSource = domain.DefaultRisk, RiskDefault
		}
		b.add(e)
	}
	return b.finish(), nil
}

// hints keeps the boolean annotations and the risk they suggest, with the
// specification's defaults for what is absent (read-only false,
// destructive true).
func hints(a map[string]any) (map[string]any, domain.RiskLevel) {
	out := map[string]any{}
	for _, k := range hintKeys {
		if v, ok := a[k].(bool); ok {
			out[k] = v
		}
	}
	if len(out) == 0 {
		return nil, ""
	}
	readOnly, _ := out["readOnlyHint"].(bool)
	destructive, set := out["destructiveHint"].(bool)
	switch {
	case readOnly:
		return out, domain.RiskRead
	case set && !destructive:
		return out, domain.RiskWriteReversible
	}
	return out, domain.RiskWriteIrreversible
}
