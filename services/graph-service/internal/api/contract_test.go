package api_test

import (
	"net/http"
	"regexp"
	"slices"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/api"
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

// TestEveryRouteIsInTheContract holds the routes the graph service serves
// and the operations its API contract documents to each other (ADR-0021).
// Traffic is checked by the integration tests.
func TestEveryRouteIsInTheContract(t *testing.T) {
	s := &api.Server{}
	top, inner := &recorder{}, &recorder{}
	s.Routes(top)
	s.APIRoutes(inner)
	var mounts []string
	for _, p := range top.patterns {
		if !strings.Contains(p, " ") {
			mounts = append(mounts, p)
		}
	}
	// Every route is mounted under /api/ behind the internal token check.
	if !slices.Equal(mounts, []string{"/api/"}) || len(top.patterns) != 1 {
		t.Fatalf("top-level routes: %v", top.patterns)
	}
	got, want := normalized(inner.patterns), normalized(contract.Routes())
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

// TestTheConsumerHandlesItsQueue holds the handlers to the queue's bindings:
// a routing key bound without a handler would be acknowledged and lost.
func TestTheConsumerHandlesItsQueue(t *testing.T) {
	handled := []string{}
	for typ := range (&api.Server{}).Handlers() {
		handled = append(handled, typ)
	}
	slices.Sort(handled)
	topology, err := events.LoadTopology()
	if err != nil {
		t.Fatal(err)
	}
	bound := slices.Clone(topology.Queues["graph-service.events"])
	slices.Sort(bound)
	if !slices.Equal(handled, bound) {
		t.Errorf("handlers %v, queue bindings %v", handled, bound)
	}
}
