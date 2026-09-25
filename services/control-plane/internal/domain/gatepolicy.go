package domain

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
)

// GatePolicy is the per-project release-gate configuration. Zero values fall
// back to the documented defaults so an empty policy is a safe policy.
type GatePolicy struct {
	// Suite selection (spec §48).
	AlwaysRunTags           []string `json:"alwaysRunTags,omitempty"`
	MaxProductionSamples    *int     `json:"maxProductionSamples,omitempty"`
	IncludeKnownRegressions *bool    `json:"includeKnownRegressions,omitempty"`
	MaxDepth                *int     `json:"maxDepth,omitempty"`
	// Budgets (spec §49).
	MaxGateCostUSD *float64 `json:"maxGateCostUsd,omitempty"`
	// WARN thresholds.
	LatencyRegressionPct   *float64 `json:"latencyRegressionPct,omitempty"`
	LatencyRegressionMinMS *float64 `json:"latencyRegressionMinMs,omitempty"`
	CostRegressionPct      *float64 `json:"costRegressionPct,omitempty"`
	SemanticRegressionDrop *float64 `json:"semanticRegressionDrop,omitempty"`
	// A judge may only produce blocking evidence once its calibration agreement
	// reaches this value (spec §16.4).
	JudgeMinAgreement *float64 `json:"judgeMinAgreement,omitempty"`
	// Governance.
	AllowReviewerOverride *bool `json:"allowReviewerOverride,omitempty"`
	WarnFailsCI           *bool `json:"warnFailsCI,omitempty"`
}

// ResolvedGatePolicy has every default applied.
type ResolvedGatePolicy struct {
	AlwaysRunTags           []string `json:"always_run_tags"`
	MaxProductionSamples    int      `json:"max_production_samples"`
	IncludeKnownRegressions bool     `json:"include_known_regressions"`
	MaxDepth                int      `json:"max_depth"`
	MaxGateCostUSD          float64  `json:"max_gate_cost_usd"`
	LatencyRegressionPct    float64  `json:"latency_regression_pct"`
	LatencyRegressionMinMS  float64  `json:"latency_regression_min_ms"`
	CostRegressionPct       float64  `json:"cost_regression_pct"`
	SemanticRegressionDrop  float64  `json:"semantic_regression_drop"`
	JudgeMinAgreement       float64  `json:"judge_min_agreement"`
	AllowReviewerOverride   bool     `json:"allow_reviewer_override"`
	WarnFailsCI             bool     `json:"warn_fails_ci"`
}

// ParseGatePolicy decodes and validates a gate policy document.
func ParseGatePolicy(raw []byte) (GatePolicy, error) {
	var gp GatePolicy
	if len(bytes.TrimSpace(raw)) == 0 {
		return gp, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&gp); err != nil {
		return gp, fmt.Errorf("invalid gate policy: %w", err)
	}
	checks := []struct {
		name     string
		v        *float64
		min, max float64
	}{
		{"maxGateCostUsd", gp.MaxGateCostUSD, 0, 100000},
		{"latencyRegressionPct", gp.LatencyRegressionPct, 0, 1000},
		{"latencyRegressionMinMs", gp.LatencyRegressionMinMS, 0, 600000},
		{"costRegressionPct", gp.CostRegressionPct, 0, 1000},
		{"semanticRegressionDrop", gp.SemanticRegressionDrop, 0, 1},
		{"judgeMinAgreement", gp.JudgeMinAgreement, 0, 1},
	}
	for _, c := range checks {
		if c.v != nil && (*c.v < c.min || *c.v > c.max) {
			return gp, fmt.Errorf("%s must be between %g and %g", c.name, c.min, c.max)
		}
	}
	if gp.MaxProductionSamples != nil && (*gp.MaxProductionSamples < 0 || *gp.MaxProductionSamples > 1000) {
		return gp, errors.New("maxProductionSamples must be between 0 and 1000")
	}
	if gp.MaxDepth != nil && (*gp.MaxDepth < 1 || *gp.MaxDepth > 8) {
		return gp, errors.New("maxDepth must be between 1 and 8")
	}
	return gp, nil
}

// Resolve applies defaults.
func (gp GatePolicy) Resolve() ResolvedGatePolicy {
	r := ResolvedGatePolicy{
		AlwaysRunTags:           []string{"critical", "security"},
		MaxProductionSamples:    100,
		IncludeKnownRegressions: true,
		MaxDepth:                4,
		MaxGateCostUSD:          20,
		LatencyRegressionPct:    25,
		LatencyRegressionMinMS:  250,
		CostRegressionPct:       15,
		SemanticRegressionDrop:  0.1,
		JudgeMinAgreement:       0.8,
		AllowReviewerOverride:   true,
		WarnFailsCI:             false,
	}
	if gp.AlwaysRunTags != nil {
		r.AlwaysRunTags = gp.AlwaysRunTags
	}
	setInt := func(dst *int, v *int) {
		if v != nil {
			*dst = *v
		}
	}
	setF := func(dst *float64, v *float64) {
		if v != nil {
			*dst = *v
		}
	}
	setB := func(dst *bool, v *bool) {
		if v != nil {
			*dst = *v
		}
	}
	setInt(&r.MaxProductionSamples, gp.MaxProductionSamples)
	setInt(&r.MaxDepth, gp.MaxDepth)
	setB(&r.IncludeKnownRegressions, gp.IncludeKnownRegressions)
	setF(&r.MaxGateCostUSD, gp.MaxGateCostUSD)
	setF(&r.LatencyRegressionPct, gp.LatencyRegressionPct)
	setF(&r.LatencyRegressionMinMS, gp.LatencyRegressionMinMS)
	setF(&r.CostRegressionPct, gp.CostRegressionPct)
	setF(&r.SemanticRegressionDrop, gp.SemanticRegressionDrop)
	setF(&r.JudgeMinAgreement, gp.JudgeMinAgreement)
	setB(&r.AllowReviewerOverride, gp.AllowReviewerOverride)
	setB(&r.WarnFailsCI, gp.WarnFailsCI)
	return r
}
