// Package jsonschemax compiles embedded JSON Schemas and turns validation
// failures into stable, user-facing field errors.
package jsonschemax

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"golang.org/x/text/language"
	"golang.org/x/text/message"
)

var printer = message.NewPrinter(language.English)

// FieldError is one validation problem at a JSON pointer location.
type FieldError struct {
	Path    string `json:"path"`
	Message string `json:"message"`
}

// ValidationError aggregates field errors.
type ValidationError struct {
	Errors []FieldError
}

func (v *ValidationError) Error() string {
	parts := make([]string, 0, len(v.Errors))
	for _, e := range v.Errors {
		parts = append(parts, e.Path+": "+e.Message)
	}
	return "schema validation failed: " + strings.Join(parts, "; ")
}

// Compile loads schemaPath from fsys and compiles it.
func Compile(fsys fs.FS, schemaPath string) (*jsonschema.Schema, error) {
	raw, err := fs.ReadFile(fsys, schemaPath)
	if err != nil {
		return nil, fmt.Errorf("read schema %s: %w", schemaPath, err)
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		return nil, fmt.Errorf("parse schema %s: %w", schemaPath, err)
	}
	c := jsonschema.NewCompiler()
	c.AssertFormat()
	url := "mem:///" + schemaPath
	if err := c.AddResource(url, doc); err != nil {
		return nil, fmt.Errorf("add schema %s: %w", schemaPath, err)
	}
	return c.Compile(url)
}

// Validate validates a decoded JSON value (from json.Unmarshal into any).
func Validate(s *jsonschema.Schema, v any) error {
	if err := s.Validate(normalize(v)); err != nil {
		var ve *jsonschema.ValidationError
		if errors.As(err, &ve) {
			return &ValidationError{Errors: flatten(ve)}
		}
		return err
	}
	return nil
}

// ValidateJSON validates raw JSON bytes.
func ValidateJSON(s *jsonschema.Schema, raw []byte) error {
	v, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		return &ValidationError{Errors: []FieldError{{Path: "/", Message: "invalid JSON: " + err.Error()}}}
	}
	return Validate(s, v)
}

// normalize converts json.Number/float64 trees produced by encoding/json into
// the representation the validator expects.
func normalize(v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		return v
	}
	out, err := jsonschema.UnmarshalJSON(bytes.NewReader(b))
	if err != nil {
		return v
	}
	return out
}

func flatten(ve *jsonschema.ValidationError) []FieldError {
	var out []FieldError
	var walk func(e *jsonschema.ValidationError)
	walk = func(e *jsonschema.ValidationError) {
		if len(e.Causes) == 0 {
			path := "/" + strings.Join(e.InstanceLocation, "/")
			out = append(out, FieldError{Path: path, Message: e.ErrorKind.LocalizedString(printer)})
			return
		}
		for _, c := range e.Causes {
			walk(c)
		}
	}
	walk(ve)
	sort.SliceStable(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	// Deduplicate identical messages (oneOf branches often repeat).
	dedup := out[:0]
	seen := map[string]bool{}
	for _, e := range out {
		k := e.Path + "|" + e.Message
		if !seen[k] {
			seen[k] = true
			dedup = append(dedup, e)
		}
	}
	return dedup
}
