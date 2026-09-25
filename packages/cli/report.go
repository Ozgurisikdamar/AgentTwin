package cli

import (
	"encoding/json"
	"encoding/xml"
	"fmt"
	"slices"
	"strings"
	"unicode/utf8"
)

// reportInput is what a report shows.
type reportInput struct {
	release Release
	gate    Gate
	rawGate json.RawMessage
	exit    int
	webURL  string
}

// maxEvidence bounds the evidence a text report lists per rule.
const maxEvidence = 5

func (r reportInput) link() string {
	if r.webURL == "" {
		return ""
	}
	return strings.TrimRight(r.webURL, "/") + "/releases/" + r.release.ID
}

func short(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n-1]) + "…"
}

// renderText is the report a person reads in a CI log (spec §101).
func renderText(r reportInput) string {
	var b strings.Builder
	w := func(format string, a ...any) { fmt.Fprintf(&b, format, a...) }
	g, d := r.gate, r.gate.Decision
	w("AgentTwin release gate\n%s\n", strings.Repeat("─", 60))
	w("%-12s %s %s → %s · revision %d\n", "Release", r.release.Agent.Name, r.release.Baseline.Version,
		r.release.Candidate.Version, g.Revision)
	if r.release.Title != "" {
		w("%-12s %s\n", "Title", r.release.Title)
	}
	if r.release.CommitSHA != nil {
		w("%-12s %s\n", "Commit", *r.release.CommitSHA)
	}
	switch o := g.Override; {
	case o != nil && o.Active:
		w("%-12s OVERRIDDEN (originally %s)\n", "Gate", o.OriginalOutcome)
		w("%-12s by %s on %s: %q\n", "Override", o.Actor, o.CreatedAt.UTC().Format("2006-01-02 15:04 UTC"), o.Reason)
		if o.TicketURL != nil {
			w("%-12s %s\n", "Ticket", *o.TicketURL)
		}
		if o.ExpiresAt != nil {
			w("%-12s %s\n", "Expires", o.ExpiresAt.UTC().Format("2006-01-02 15:04 UTC"))
		}
	case o != nil:
		w("%-12s %s (an override expired on %s)\n", "Gate", d.Outcome, o.ExpiresAt.UTC().Format("2006-01-02 15:04 UTC"))
	default:
		w("%-12s %s\n", "Gate", d.Outcome)
	}
	if d.Incomplete {
		w("%-12s evidence is missing: the gate cannot pass\n", "")
	}
	for _, line := range wrap(d.Summary, 86) {
		w("%-12s %s\n", "", line)
	}
	w("\n")

	for _, c := range d.Coverage {
		if c.Total == 0 {
			continue
		}
		line := fmt.Sprintf("%-44s %d/%d", c.Name, c.Covered, c.Total)
		if len(c.Missing) > 0 {
			line += "   missing: " + short(strings.Join(c.Missing, ", "), 80)
		}
		w("%s\n", line)
	}
	for _, rule := range d.Rules {
		w("\n%-6s %s (%s)\n", rule.Outcome, rule.Title, rule.Rule)
		w("       %s\n", rule.Statement)
		last := ""
		for i, ev := range rule.Evidence {
			if i == maxEvidence {
				w("       … and %d more (see --format json)\n", len(rule.Evidence)-maxEvidence)
				break
			}
			what := ev.Candidate
			if what == "" || what == "passed" {
				what = ev.Summary
			}
			switch {
			case ev.Scenario != "" && ev.Expectation != "":
				w("       • %s (%s): %s\n", ev.Scenario, ev.Expectation, short(what, 160))
			case ev.Scenario != "":
				w("       • %s: %s\n", ev.Scenario, short(what, 160))
			default:
				w("       • %s\n", short(what, 160))
			}
			if ev.PreExisting {
				w("         the baseline fails the same way\n")
			}
			// Where the run diverged and its trace belong to the scenario:
			// said once for consecutive evidence of one scenario.
			if ev.Scenario == "" || ev.Scenario != last {
				if ev.Divergence != "" {
					w("         first divergence: %s\n", short(ev.Divergence, 160))
				}
				if ev.TraceID != "" {
					w("         trace: %s\n", ev.TraceID)
				}
			}
			last = ev.Scenario
		}
	}
	w("\n%-12s %d/100 (sorts releases; never decides)\n", "Risk index", d.RiskIndex.Value)
	if g.EvidenceSHA256 != nil {
		state := "verified"
		if g.EvidenceVerified == nil || !*g.EvidenceVerified {
			state = "NOT VERIFIED: the stored decision no longer matches its hash"
		}
		w("%-12s sha256 %s (%s)\n", "Evidence", *g.EvidenceSHA256, state)
	}
	if link := r.link(); link != "" {
		w("%-12s %s\n", "Details", link)
	}
	w("%-12s %s\n", "Release id", r.release.ID)
	w("\nExit code: %d\n", r.exit)
	return b.String()
}

// wrap splits text into lines of at most width runes, at spaces.
func wrap(text string, width int) []string {
	var lines []string
	line := ""
	for _, word := range strings.Fields(text) {
		switch {
		case line == "":
			line = word
		case utf8.RuneCountInString(line)+1+utf8.RuneCountInString(word) > width:
			lines = append(lines, line)
			line = word
		default:
			line += " " + word
		}
	}
	if line != "" {
		lines = append(lines, line)
	}
	return lines
}

// renderJSON is the machine-readable report: the release, the gate as the
// API answered it, and the CLI's exit code.
func renderJSON(r reportInput) ([]byte, error) {
	out, err := json.MarshalIndent(map[string]any{
		"release": r.release, "gate": r.rawGate, "exit_code": r.exit, "details_url": nullable(r.link()),
	}, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(out, '\n'), nil
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

type junitSuites struct {
	XMLName  xml.Name     `xml:"testsuites"`
	Name     string       `xml:"name,attr"`
	Tests    int          `xml:"tests,attr"`
	Failures int          `xml:"failures,attr"`
	Suites   []junitSuite `xml:"testsuite"`
}

type junitSuite struct {
	Name       string          `xml:"name,attr"`
	Tests      int             `xml:"tests,attr"`
	Failures   int             `xml:"failures,attr"`
	Properties []junitProperty `xml:"properties>property"`
	Cases      []junitCase     `xml:"testcase"`
}

type junitProperty struct {
	Name  string `xml:"name,attr"`
	Value string `xml:"value,attr"`
}

type junitCase struct {
	Name      string        `xml:"name,attr"`
	Classname string        `xml:"classname,attr"`
	Failure   *junitFailure `xml:"failure,omitempty"`
	SystemOut string        `xml:"system-out,omitempty"`
}

type junitFailure struct {
	Message string `xml:"message,attr"`
	Type    string `xml:"type,attr"`
	Text    string `xml:",chardata"`
}

// evidenceText is one piece of evidence as a JUnit line.
func evidenceText(rule Rule, ev Evidence) string {
	what := ev.Candidate
	if what == "" || what == "passed" {
		what = ev.Summary
	}
	parts := []string{fmt.Sprintf("[%s] %s: %s", rule.Outcome, rule.Title, what)}
	if ev.Expectation != "" {
		parts = append(parts, "expectation: "+ev.Expectation)
	}
	if ev.Divergence != "" {
		parts = append(parts, "first divergence: "+ev.Divergence)
	}
	if ev.TraceID != "" {
		parts = append(parts, "trace: "+ev.TraceID)
	}
	return strings.Join(parts, "\n  ")
}

// renderJUnit is the report CI systems show as tests: one test for the
// gate, one per scenario of the suite, and one per rule whose evidence
// names no scenario. A BLOCK fails its tests; a WARN fails them only when
// it fails CI.
func renderJUnit(r reportInput, ci bool) ([]byte, error) {
	g, d := r.gate, r.gate.Decision
	// Outside CI a warning fails; in CI, when the gate policy says so.
	warnFails := !ci || g.Policy.WarnFailsCI
	failing := func(outcome string) bool { return outcome == "BLOCK" || outcome == "WARN" && warnFails }
	if g.Override != nil && g.Override.Active {
		failing = func(string) bool { return false }
	}
	type findings struct{ fail, note []string }
	byScenario := map[string]*findings{}
	var order []string
	scenario := func(name string) *findings {
		if byScenario[name] == nil {
			byScenario[name] = &findings{}
			order = append(order, name)
		}
		return byScenario[name]
	}
	for _, s := range g.Suite {
		scenario(s.ScenarioName)
	}
	var ruleCases []junitCase
	for _, rule := range d.Rules {
		var general []string
		for _, ev := range rule.Evidence {
			if ev.Scenario == "" {
				general = append(general, evidenceText(rule, ev))
				continue
			}
			f := scenario(ev.Scenario)
			if failing(rule.Outcome) {
				f.fail = append(f.fail, evidenceText(rule, ev))
			} else {
				f.note = append(f.note, evidenceText(rule, ev))
			}
		}
		if len(general) == 0 {
			continue
		}
		c := junitCase{Name: rule.Rule, Classname: "agenttwin.rule"}
		text := rule.Statement + "\n" + strings.Join(general, "\n")
		if failing(rule.Outcome) {
			c.Failure = &junitFailure{Message: rule.Title, Type: rule.Outcome, Text: text}
		} else {
			c.SystemOut = text
		}
		ruleCases = append(ruleCases, c)
	}

	gateCase := junitCase{Name: "gate", Classname: "agenttwin.release"}
	gateText := d.Summary
	if o := g.Override; o != nil && o.Active {
		gateText = fmt.Sprintf("Originally %s; overridden by %s: %s\n%s", o.OriginalOutcome, o.Actor, o.Reason, d.Summary)
	}
	if r.exit != ExitPass {
		gateCase.Failure = &junitFailure{Message: fmt.Sprintf("Gate %s (exit %d)", d.Outcome, r.exit), Type: d.Outcome, Text: gateText}
	} else {
		gateCase.SystemOut = gateText
	}
	cases := []junitCase{gateCase}
	for _, name := range order {
		f := byScenario[name]
		c := junitCase{Name: name, Classname: "agenttwin.scenario"}
		if len(f.fail) > 0 {
			c.Failure = &junitFailure{Message: short(strings.SplitN(f.fail[0], "\n", 2)[0], 200), Type: "BLOCK",
				Text: strings.Join(f.fail, "\n")}
		}
		if len(f.note) > 0 {
			c.SystemOut = strings.Join(f.note, "\n")
		}
		cases = append(cases, c)
	}
	cases = append(cases, ruleCases...)
	failures := 0
	for _, c := range cases {
		if c.Failure != nil {
			failures++
		}
	}
	props := []junitProperty{{"release_id", r.release.ID}, {"revision", fmt.Sprint(g.Revision)},
		{"outcome", d.Outcome}, {"effective_outcome", g.EffectiveOutcome}, {"exit_code", fmt.Sprint(r.exit)},
		{"risk_index", fmt.Sprint(d.RiskIndex.Value)}, {"rules_version", d.RulesVersion}}
	if g.EvidenceSHA256 != nil {
		props = append(props, junitProperty{"evidence_sha256", *g.EvidenceSHA256})
	}
	if link := r.link(); link != "" {
		props = append(props, junitProperty{"details_url", link})
	}
	name := fmt.Sprintf("agenttwin release gate: %s %s → %s", r.release.Agent.Name, r.release.Baseline.Version,
		r.release.Candidate.Version)
	doc := junitSuites{Name: "agenttwin", Tests: len(cases), Failures: failures, Suites: []junitSuite{{
		Name: name, Tests: len(cases), Failures: failures, Properties: props, Cases: cases}}}
	out, err := xml.MarshalIndent(doc, "", "  ")
	if err != nil {
		return nil, err
	}
	return slices.Concat([]byte(xml.Header), out, []byte("\n")), nil
}

func sortedStrings(s []string) []string {
	slices.Sort(s)
	return s
}
