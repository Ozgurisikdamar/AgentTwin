package api_test

import (
	"net/http"
	"regexp"
	"slices"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/api"
)

// recorder records the patterns routes are registered with.
type recorder struct{ patterns []string }

func (r *recorder) Handle(pattern string, _ http.Handler) { r.patterns = append(r.patterns, pattern) }

func (r *recorder) HandleFunc(pattern string, _ func(http.ResponseWriter, *http.Request)) {
	r.patterns = append(r.patterns, pattern)
}

var pathParam = regexp.MustCompile(`\{[^}]*\}`)

// normalized drops the names of path parameters.
func normalized(routes []string) []string {
	out := make([]string, 0, len(routes))
	for _, r := range routes {
		out = append(out, pathParam.ReplaceAllString(r, "{}"))
	}
	slices.Sort(out)
	return out
}

// TestEveryRouteIsInTheContract holds the routes the trace service serves
// and the operations its API contract documents to each other (ADR-0021): an
// undocumented route and a documented operation the service does not serve
// both fail. Traffic is checked by the integration tests.
func TestEveryRouteIsInTheContract(t *testing.T) {
	s := &api.Server{}
	top, query := &recorder{}, &recorder{}
	s.Routes(top)
	s.QueryRoutes(query)
	var served, mounts []string
	for _, p := range append(top.patterns, query.patterns...) {
		if strings.Contains(p, " ") {
			served = append(served, p)
		} else {
			mounts = append(mounts, p)
		}
	}
	// The query routes are mounted under /api/ behind the internal token check.
	if !slices.Equal(mounts, []string{"/api/"}) {
		t.Fatalf("routes without a method: %v", mounts)
	}
	got, want := normalized(served), normalized(contract.Routes())
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
