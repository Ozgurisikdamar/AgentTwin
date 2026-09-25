package api

import (
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
)

// Metrics of the runtime gateway (spec §54). Labels are bounded: effects and
// outcomes are fixed sets; tools, agents and policies never become labels
// (they are user-authored), so a noisy tenant cannot explode the series.
type Metrics struct {
	Decisions *prometheus.CounterVec
	Upstream  *prometheus.HistogramVec
	Approvals *prometheus.CounterVec
	Pending   prometheus.Gauge
}

// Approval events counted by Metrics.Approval.
const (
	ApprovalRequested = "requested"
	ApprovalApproved  = "approved"
	ApprovalDenied    = "denied"
	ApprovalClaimed   = "token_claimed"
	ApprovalUsed      = "used"
	ApprovalRefused   = "token_refused"
)

var (
	knownEffects  = map[string]bool{"allow": true, "allow_with_limits": true, "require_approval": true, "deny": true}
	knownOutcomes = map[string]bool{
		store.OutcomeExecuted: true, store.OutcomeFailed: true, store.OutcomeReplayed: true,
		store.OutcomeDenied: true, store.OutcomeApprovalRequired: true, store.OutcomeApprovalRefused: true,
		"refused": true, "unavailable": true,
	}
	knownApprovalEvents = map[string]bool{ApprovalRequested: true, ApprovalApproved: true, ApprovalDenied: true,
		ApprovalClaimed: true, ApprovalUsed: true, ApprovalRefused: true}
)

// NewMetrics registers the gateway's metrics.
func NewMetrics(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		Decisions: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "agenttwin_runtime_decisions_total",
			Help: "Tool calls the gateway decided, by effect and final outcome."}, []string{"effect", "outcome"}),
		Upstream: prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "agenttwin_runtime_upstream_duration_seconds",
			Help:    "Latency of forwarded tool calls, by outcome (executed, failed).",
			Buckets: prometheus.ExponentialBuckets(0.005, 2, 14)}, []string{"outcome"}),
		Approvals: prometheus.NewCounterVec(prometheus.CounterOpts{Name: "agenttwin_runtime_approvals_total",
			Help: "Approval events: requested, approved, denied, token_claimed, used, token_refused."}, []string{"event"}),
		Pending: prometheus.NewGauge(prometheus.GaugeOpts{Name: "agenttwin_runtime_approvals_pending",
			Help: "Approval requests waiting for a person and not yet past their expiry."}),
	}
	reg.MustRegister(m.Decisions, m.Upstream, m.Approvals, m.Pending)
	return m
}

// Decision counts one decided call. A nil *Metrics records nothing.
func (m *Metrics) Decision(effect *string, outcome string) {
	if m == nil {
		return
	}
	e := "none"
	if effect != nil && knownEffects[*effect] {
		e = *effect
	}
	if !knownOutcomes[outcome] {
		outcome = "other"
	}
	m.Decisions.WithLabelValues(e, outcome).Inc()
}

// Forwarded records the tool's latency for a forwarded call.
func (m *Metrics) Forwarded(outcome string, d time.Duration) {
	if m == nil {
		return
	}
	m.Upstream.WithLabelValues(outcome).Observe(d.Seconds())
}

// Approval counts one approval event.
func (m *Metrics) Approval(event string) {
	if m == nil || !knownApprovalEvents[event] {
		return
	}
	m.Approvals.WithLabelValues(event).Inc()
}

// SetPending sets the number of approval requests waiting for a person.
func (m *Metrics) SetPending(n int) {
	if m == nil {
		return
	}
	m.Pending.Set(float64(n))
}
