// Package scenarioschema embeds the user-facing document schemas (agent
// manifest, scenario, runtime policy, tool twin).
package scenarioschema

import "embed"

// Schemas holds schemas/*.schema.json.
//
//go:embed schemas/*.json
var Schemas embed.FS
