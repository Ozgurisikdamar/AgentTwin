package api

import (
	"net/http"
	"regexp"
	"slices"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/openapicheck"
)

// recorder records the patterns routes are registered with.
type recorder struct{ patterns []string }

func (r *recorder) Handle(pattern string, _ http.Handler) { r.patterns = append(r.patterns, pattern) }

func (r *recorder) HandleFunc(pattern string, _ func(http.ResponseWriter, *http.Request)) {
	r.patterns = append(r.patterns, pattern)
}

var pathParam = regexp.MustCompile(`\{[^}]*\}`)

// normalized drops the names of path parameters: the service's `{id}` is the
// contract's `{project_id}`.
func normalized(routes []string) []string {
	out := make([]string, 0, len(routes))
	for _, r := range routes {
		out = append(out, pathParam.ReplaceAllString(r, "{}"))
	}
	slices.Sort(out)
	return out
}

// TestEveryRouteIsInTheContract holds the routes the control plane serves and
// the operations its API contract documents to each other (ADR-0021): an
// undocumented route and a documented operation the service does not serve
// both fail. Traffic is checked by the integration tests.
func TestEveryRouteIsInTheContract(t *testing.T) {
	c, err := openapicheck.Load(contracts.OpenAPI, "openapi/control-plane.openapi.yaml")
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{}
	rec := &recorder{}
	s.PublicRoutes(rec)
	s.InternalRoutes(rec)
	var served, catchAll []string
	for _, p := range rec.patterns {
		if strings.Contains(p, " ") {
			served = append(served, p)
		} else {
			catchAll = append(catchAll, p)
		}
	}
	// The rest of /api/v1 is forwarded to the owning services, which document it.
	if !slices.Equal(catchAll, []string{"/api/v1/"}) {
		t.Fatalf("routes without a method: %v", catchAll)
	}
	got, want := normalized(served), normalized(c.Routes())
	for _, r := range got {
		if !slices.Contains(want, r) {
			t.Errorf("served but not documented: %s", r)
		}
	}
	for _, r := range want {
		if !slices.Contains(got, r) {
			t.Errorf("documented but not served: %s", r)
		}
	}
	if len(got) != len(want) {
		t.Errorf("%d routes served, %d documented", len(got), len(want))
	}
}
