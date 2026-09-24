// Package ids generates identifiers. UUIDv7 keeps primary keys time-ordered,
// which keeps B-tree inserts local and makes cursor pagination natural.
package ids

import "github.com/google/uuid"

// New returns a new UUIDv7 string.
func New() string {
	id, err := uuid.NewV7()
	if err != nil {
		// NewV7 only fails when the system random source fails.
		return uuid.NewString()
	}
	return id.String()
}

// Valid reports whether s is a syntactically valid UUID.
func Valid(s string) bool {
	_, err := uuid.Parse(s)
	return err == nil && len(s) == 36
}
