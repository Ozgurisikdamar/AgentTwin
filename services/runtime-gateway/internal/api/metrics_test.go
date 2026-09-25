package api_test

import (
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	promtest "github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/api"
)

func TestMetricsKeepLabelsBounded(t *testing.T) {
	m := api.NewMetrics(prometheus.NewRegistry())
	effect, odd := "deny", "made-up-effect"
	m.Decision(&effect, "denied")
	m.Decision(&odd, "some-new-outcome")
	m.Decision(nil, "refused")
	m.Approval("requested")
	m.Approval("not-an-event")
	m.Forwarded("executed", 250*time.Millisecond)
	m.SetPending(3)

	for _, c := range []struct {
		effect, outcome string
		want            float64
	}{{"deny", "denied", 1}, {"none", "other", 1}, {"none", "refused", 1}} {
		if got := promtest.ToFloat64(m.Decisions.WithLabelValues(c.effect, c.outcome)); got != c.want {
			t.Errorf("decisions{%s,%s} = %v, want %v", c.effect, c.outcome, got, c.want)
		}
	}
	// An unknown effect or outcome never becomes a label of its own.
	if n := promtest.CollectAndCount(m.Decisions); n != 3 {
		t.Errorf("decision series %d, want 3", n)
	}
	if n := promtest.CollectAndCount(m.Approvals); n != 1 {
		t.Errorf("approval series %d, want 1 (unknown events are dropped)", n)
	}
	if got := promtest.ToFloat64(m.Pending); got != 3 {
		t.Errorf("pending %v", got)
	}
	if n := promtest.CollectAndCount(m.Upstream); n != 1 {
		t.Errorf("upstream series %d", n)
	}
}

func TestNilMetricsRecordNothing(t *testing.T) {
	var m *api.Metrics
	effect := "allow"
	m.Decision(&effect, "executed")
	m.Approval("requested")
	m.Forwarded("executed", time.Second)
	m.SetPending(1)
}
