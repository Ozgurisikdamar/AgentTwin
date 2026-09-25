// Package contracts embeds the versioned event JSON Schemas and the HTTP API
// documents (OpenAPI 3.1) so Go services validate and test against the same
// documents that Python services and the docs reference.
package contracts

import "embed"

// Events holds events/*.schema.json.
//
//go:embed events/*.schema.json
var Events embed.FS

// Fixtures holds shared cross-language test fixtures.
//
//go:embed fixtures/*.json
var Fixtures embed.FS

// Topology is the RabbitMQ topology shared by all services (topology.json).
//
//go:embed topology.json
var Topology []byte

// OpenAPI holds the HTTP API documents, openapi/<service>.openapi.yaml
// (ADR-0021).
//
//go:embed openapi/*.openapi.yaml
var OpenAPI embed.FS
