// Package contracts embeds the versioned event JSON Schemas so Go services
// validate the same documents that Python services and the docs reference.
package contracts

import "embed"

// Events holds events/*.schema.json.
//
//go:embed events/*.json
var Events embed.FS

// Fixtures holds shared cross-language test fixtures.
//
//go:embed fixtures/*.json
var Fixtures embed.FS

// Topology is the RabbitMQ topology shared by all services (topology.json).
//
//go:embed topology.json
var Topology []byte
