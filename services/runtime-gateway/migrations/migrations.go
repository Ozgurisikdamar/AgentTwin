// Package migrations embeds the runtime schema migrations.
package migrations

import "embed"

// FS holds NNNN_name.(up|down).sql files.
//
//go:embed *.sql
var FS embed.FS

// Schema is the PostgreSQL schema owned by the runtime-gateway.
const Schema = "runtime"
