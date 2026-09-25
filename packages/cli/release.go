package cli

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strings"
	"time"
)

// Release is a release as the API answers it (the parts the CLI reads).
type Release struct {
	ID    string `json:"id"`
	Agent struct {
		Name string `json:"name"`
	} `json:"agent"`
	Baseline struct {
		Version string `json:"version"`
	} `json:"baseline"`
	Candidate struct {
		Version string `json:"version"`
	} `json:"candidate"`
	Title     string  `json:"title"`
	CommitSHA *string `json:"commit_sha"`
	CIURL     *string `json:"ci_url"`
}

// Evidence is a reason a gate rule fired.
type Evidence struct {
	Scenario    string `json:"scenario"`
	Expectation string `json:"expectation"`
	Summary     string `json:"summary"`
	Candidate   string `json:"candidate"`
	Baseline    string `json:"baseline"`
	Divergence  string `json:"divergence"`
	TraceID     string `json:"trace_id"`
	PreExisting bool   `json:"pre_existing"`
}

// Rule is a gate rule that fired.
type Rule struct {
	Rule      string     `json:"rule"`
	Outcome   string     `json:"outcome"`
	Title     string     `json:"title"`
	Statement string     `json:"statement"`
	Evidence  []Evidence `json:"evidence"`
}

// Ratio is a coverage ratio of the gate.
type Ratio struct {
	Name    string   `json:"name"`
	Covered int      `json:"covered"`
	Total   int      `json:"total"`
	Missing []string `json:"missing"`
}

// Decision is the gate's decision.
type Decision struct {
	Outcome      string         `json:"outcome"`
	Incomplete   bool           `json:"incomplete"`
	RulesVersion string         `json:"rules_version"`
	Summary      string         `json:"summary"`
	Rules        []Rule         `json:"rules"`
	Counts       map[string]int `json:"counts"`
	Coverage     []Ratio        `json:"coverage"`
	RiskIndex    struct {
		Value int `json:"value"`
	} `json:"risk_index"`
}

// Override records who let a gated release through.
type Override struct {
	OriginalOutcome string     `json:"original_outcome"`
	Reason          string     `json:"reason"`
	TicketURL       *string    `json:"ticket_url"`
	ExpiresAt       *time.Time `json:"expires_at"`
	Actor           string     `json:"actor"`
	CreatedAt       time.Time  `json:"created_at"`
	Active          bool       `json:"active"`
}

// Gate is a release's gate as the API answers it.
type Gate struct {
	ReleaseID    string `json:"release_id"`
	EvaluationID string `json:"release_evaluation_id"`
	Revision     int    `json:"revision"`
	Status       string `json:"status"`
	Suite        []struct {
		ScenarioName string `json:"scenario_name"`
		Severity     string `json:"severity"`
	} `json:"suite"`
	Policy struct {
		WarnFailsCI bool `json:"warn_fails_ci"`
	} `json:"policy"`
	Decision         *Decision `json:"decision"`
	EvidenceSHA256   *string   `json:"evidence_sha256"`
	EvidenceVerified *bool     `json:"evidence_verified"`
	Override         *Override `json:"override"`
	EffectiveOutcome string    `json:"effective_outcome"`
	ExitCode         *int      `json:"exit_code"`
	CIFails          *bool     `json:"ci_fails"`
}

// Decided reports whether the gate has decided.
func (g Gate) Decided() bool { return g.Status == "DECIDED" && g.Decision != nil && g.ExitCode != nil }

// ExitFor is the CLI's exit code for a decided gate: the API's (0 pass,
// 2 warn, 3 block, 0 while overridden), except that in CI a warning the
// gate policy does not fail CI passes.
func ExitFor(g Gate, ci bool) int {
	if !g.Decided() {
		return ExitError
	}
	code := *g.ExitCode
	if ci && code == ExitWarn && g.CIFails != nil && !*g.CIFails {
		return ExitPass
	}
	return code
}

var (
	commitPattern = regexp.MustCompile(`^[a-f0-9]{7,64}$`)
	agentPattern  = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	uuidPattern   = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
)

// maxChangedFiles is what the API accepts in one change set.
const maxChangedFiles = 1000

// checkOptions is what "release check" was asked.
type checkOptions struct {
	common
	project, agent, baseline, candidate string
	title, commit, baseCommit, ciURL    string
	changedFiles                        string
	release                             string
	reevaluate, ci, noWait              bool
	format, output, webURL              string
	timeout, pollInterval               time.Duration
}

func (o *checkOptions) register(fs *flag.FlagSet, env Env) {
	o.common.register(fs, env)
	fs.StringVar(&o.project, "project", env.Getenv("AGENTTWIN_PROJECT"), "the project, by id or slug (AGENTTWIN_PROJECT)")
	fs.StringVar(&o.agent, "agent", "", "the agent (or give AGENT@VERSION to --baseline and --candidate)")
	fs.StringVar(&o.baseline, "baseline", "", "the version in use: VERSION, or AGENT@VERSION")
	fs.StringVar(&o.candidate, "candidate", "", "the version to release: VERSION, or AGENT@VERSION")
	fs.StringVar(&o.title, "title", "", "a title for the release")
	fs.StringVar(&o.commit, "commit", "", "the candidate's git commit (default: $GITHUB_SHA, $CI_COMMIT_SHA, $BUILDKITE_COMMIT or $GIT_COMMIT)")
	fs.StringVar(&o.baseCommit, "base-commit", "", "the baseline's git commit")
	fs.StringVar(&o.changedFiles, "changed-files", "", "a file listing the changed files, one per line (- for stdin)")
	fs.StringVar(&o.ciURL, "ci-url", "", "the CI run (default: detected on GitHub Actions, GitLab, Jenkins and Buildkite)")
	fs.StringVar(&o.release, "release", "", "check an existing release by id instead of creating one")
	fs.BoolVar(&o.reevaluate, "reevaluate", false, "start a new evaluation of the release even if it has one")
	fs.BoolVar(&o.ci, "ci", false, "CI mode: a warning the gate policy does not fail CI exits 0")
	fs.BoolVar(&o.noWait, "no-wait", false, "do not wait for the gate; exit 0 once the evaluation is requested")
	fs.StringVar(&o.format, "format", "text", "the report: text, json or junit")
	fs.StringVar(&o.output, "output", "", "write the report to this file (the text report still goes to stdout)")
	fs.StringVar(&o.webURL, "web-url", env.Getenv("AGENTTWIN_WEB_URL"), "the web app, to link the report to the release (AGENTTWIN_WEB_URL)")
	fs.DurationVar(&o.timeout, "timeout", 30*time.Minute, "how long to wait for the gate")
	fs.DurationVar(&o.pollInterval, "poll-interval", 5*time.Second, "how often to ask for the gate")
}

// splitRef splits AGENT@VERSION.
func splitRef(ref string) (agent, version string) {
	if a, v, ok := strings.Cut(ref, "@"); ok {
		return a, v
	}
	return "", ref
}

// resolveVersions settles the agent and both versions from the flags.
func (o *checkOptions) resolveVersions() error {
	ba, bv := splitRef(o.baseline)
	ca, cv := splitRef(o.candidate)
	agent := o.agent
	for _, a := range []string{ba, ca} {
		switch {
		case a == "":
		case agent == "":
			agent = a
		case a != agent:
			return usageError(fmt.Sprintf("--baseline and --candidate name different agents (%s, %s)", agent, a))
		}
	}
	switch {
	case agent == "":
		return usageError("the agent is required: --agent NAME, or --baseline NAME@VERSION")
	case !agentPattern.MatchString(agent):
		return usageError(fmt.Sprintf("%q is not an agent name", agent))
	case bv == "" || cv == "":
		return usageError("--baseline and --candidate are required")
	case bv == cv:
		return usageError("--baseline and --candidate are the same version")
	}
	o.agent, o.baseline, o.candidate = agent, bv, cv
	return nil
}

// ciDefaults fills the commit and the CI run from the CI system's
// environment when the flags leave them out.
func (o *checkOptions) ciDefaults(env Env) {
	if o.commit == "" {
		for _, k := range []string{"GITHUB_SHA", "CI_COMMIT_SHA", "BUILDKITE_COMMIT", "GIT_COMMIT"} {
			if v := strings.ToLower(strings.TrimSpace(env.Getenv(k))); commitPattern.MatchString(v) {
				o.commit = v
				break
			}
		}
	}
	if o.ciURL == "" {
		if s, r, id := env.Getenv("GITHUB_SERVER_URL"), env.Getenv("GITHUB_REPOSITORY"), env.Getenv("GITHUB_RUN_ID"); s != "" && r != "" && id != "" {
			o.ciURL = s + "/" + r + "/actions/runs/" + id
		} else {
			for _, k := range []string{"CI_JOB_URL", "BUILD_URL", "BUILDKITE_BUILD_URL"} {
				if v := env.Getenv(k); v != "" {
					o.ciURL = v
					break
				}
			}
		}
	}
}

func (o *checkOptions) validate() error {
	switch o.format {
	case "text", "json", "junit":
	default:
		return usageError(fmt.Sprintf("--format must be text, json or junit, not %q", o.format))
	}
	if o.timeout <= 0 || o.pollInterval <= 0 {
		return usageError("--timeout and --poll-interval must be positive")
	}
	if o.release != "" {
		if !uuidPattern.MatchString(o.release) {
			return usageError(fmt.Sprintf("--release must be a release id, got %q", o.release))
		}
		return nil
	}
	if o.project == "" {
		return usageError("the project is required: --project (or AGENTTWIN_PROJECT)")
	}
	if err := o.resolveVersions(); err != nil {
		return err
	}
	for flagName, c := range map[string]*string{"--commit": &o.commit, "--base-commit": &o.baseCommit} {
		*c = strings.ToLower(strings.TrimSpace(*c))
		if *c != "" && !commitPattern.MatchString(*c) {
			return usageError(fmt.Sprintf("%s must be a git commit (7 to 64 hex characters), got %q", flagName, *c))
		}
	}
	if o.ciURL != "" {
		if u, err := url.Parse(o.ciURL); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return usageError(fmt.Sprintf("--ci-url must be an http(s) URL, got %q", o.ciURL))
		}
	}
	return nil
}

// readChangedFiles reads the changed file list.
func (o *checkOptions) readChangedFiles(env Env) ([]string, error) {
	if o.changedFiles == "" {
		return nil, nil
	}
	var r io.Reader
	if o.changedFiles == "-" {
		r = env.Stdin
	} else {
		f, err := os.Open(o.changedFiles)
		if err != nil {
			return nil, usageError(fmt.Sprintf("--changed-files: %v", err))
		}
		defer func() { _ = f.Close() }()
		r = f
	}
	var files []string
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		if line := strings.TrimSpace(sc.Text()); line != "" && len(line) <= 512 && !slices.Contains(files, line) {
			files = append(files, line)
		}
	}
	if err := sc.Err(); err != nil {
		return nil, usageError(fmt.Sprintf("--changed-files: %v", err))
	}
	if len(files) > maxChangedFiles {
		_, _ = fmt.Fprintf(env.Stderr, "agenttwin: %d changed files; the release records the first %d\n", len(files), maxChangedFiles)
		files = files[:maxChangedFiles]
	}
	return files, nil
}

// projectID resolves --project (an id, or a slug of a project the
// credential can read).
func projectID(ctx context.Context, c *Client, project string) (string, error) {
	if uuidPattern.MatchString(project) {
		return strings.ToLower(project), nil
	}
	var page struct {
		Items []struct {
			ID   string `json:"id"`
			Slug string `json:"slug"`
		} `json:"items"`
	}
	if err := c.do(ctx, request{method: "GET", path: "/api/v1/projects"}, &page); err != nil {
		return "", err
	}
	for _, p := range page.Items {
		if p.Slug == project {
			return p.ID, nil
		}
	}
	return "", fmt.Errorf("no project %q among the projects this credential can read", project)
}

// idempotencyKey names one release request: a retried CI job gets the
// release its first attempt created.
func idempotencyKey(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return "agenttwin-release-" + hex.EncodeToString(sum[:16])
}

// Retries of a transient failure: a few, backing off, so that a wrong URL
// or a control plane that is down fails the job in seconds, not at the
// timeout.
const (
	maxAttempts = 5
	maxBackoff  = 30 * time.Second
)

// backoff is the wait before attempt n+1 (n ≥ 1), starting at first.
func backoff(first time.Duration, n int) time.Duration {
	d := first
	for range n - 1 {
		if d >= maxBackoff/2 {
			return maxBackoff
		}
		d *= 2
	}
	return min(d, maxBackoff)
}

// retry repeats f while it fails transiently, at most maxAttempts times.
func retry(ctx context.Context, env Env, first time.Duration, f func() error) error {
	for n := 1; ; n++ {
		err := f()
		if err == nil || !Transient(err) || n == maxAttempts {
			return err
		}
		_, _ = fmt.Fprintf(env.Stderr, "agenttwin: %v; retrying\n", err)
		if serr := env.Sleep(ctx, backoff(first, n)); serr != nil {
			return fmt.Errorf("%w (and then %w)", err, serr)
		}
	}
}

func releaseCheck(ctx context.Context, args []string, env Env) (int, error) {
	var o checkOptions
	fs := newFlags("agenttwin release check", env)
	o.register(fs, env)
	if err := parse(fs, args); err != nil {
		return ExitError, err
	}
	if fs.NArg() > 0 {
		return ExitError, usageError(fmt.Sprintf("unexpected argument %q", fs.Arg(0)))
	}
	o.ciDefaults(env)
	if err := o.validate(); err != nil {
		return ExitError, err
	}
	c, err := o.client(env)
	if err != nil {
		return ExitError, err
	}
	ctx, cancel := context.WithTimeout(ctx, o.timeout)
	defer cancel()

	var rel Release
	if o.release != "" {
		var detail struct {
			Release Release `json:"release"`
		}
		if err := retry(ctx, env, o.pollInterval, func() error {
			return c.do(ctx, request{method: "GET", path: "/api/v1/releases/" + o.release}, &detail)
		}); err != nil {
			return ExitError, err
		}
		rel = detail.Release
	} else {
		if rel, err = o.create(ctx, env, c); err != nil {
			return ExitError, err
		}
	}
	if o.reevaluate || o.release != "" && !o.hasEvaluation(ctx, env, c, rel.ID) {
		key := idempotencyKey("evaluate", rel.ID, env.Now().UTC().Format(time.RFC3339Nano))
		if err := retry(ctx, env, o.pollInterval, func() error {
			return c.do(ctx, request{method: "POST", path: "/api/v1/releases/" + rel.ID + "/evaluate",
				headers: map[string]string{"Idempotency-Key": key}}, nil)
		}); err != nil {
			return ExitError, err
		}
	}
	_, _ = fmt.Fprintf(env.Stderr, "agenttwin: release %s (%s %s → %s)\n", rel.ID, rel.Agent.Name, rel.Baseline.Version, rel.Candidate.Version)
	if o.noWait {
		return ExitPass, nil
	}
	raw, g, err := o.wait(ctx, env, c, rel.ID)
	if err != nil {
		return ExitError, err
	}
	code := ExitFor(g, o.ci)
	r := reportInput{release: rel, gate: g, rawGate: raw, exit: code, webURL: o.webURL}
	if err := o.write(env, r); err != nil {
		return ExitError, err
	}
	return code, nil
}

// create creates the release, once per CI attempt.
func (o *checkOptions) create(ctx context.Context, env Env, c *Client) (Release, error) {
	pid, err := projectID(ctx, c, o.project)
	if err != nil {
		return Release{}, err
	}
	files, err := o.readChangedFiles(env)
	if err != nil {
		return Release{}, err
	}
	body := map[string]any{"project_id": pid, "agent": o.agent, "baseline_version": o.baseline,
		"candidate_version": o.candidate}
	if o.title != "" {
		body["title"] = o.title
	}
	if o.ciURL != "" {
		body["ci_url"] = o.ciURL
	}
	git := map[string]any{}
	if o.commit != "" {
		git["candidate_commit"] = o.commit
	}
	if o.baseCommit != "" {
		git["base_commit"] = o.baseCommit
	}
	if len(files) > 0 {
		git["changed_files"] = files
	}
	if len(git) > 0 {
		body["git"] = git
	}
	raw, _ := json.Marshal(body)
	key := idempotencyKey("create", string(raw))
	var created struct {
		Release Release `json:"release"`
	}
	err = retry(ctx, env, o.pollInterval, func() error {
		return c.do(ctx, request{method: "POST", path: "/api/v1/releases", body: body,
			headers: map[string]string{"Idempotency-Key": key}}, &created)
	})
	return created.Release, err
}

// hasEvaluation reports whether a release was evaluated.
func (o *checkOptions) hasEvaluation(ctx context.Context, env Env, c *Client, id string) bool {
	err := retry(ctx, env, o.pollInterval, func() error {
		return c.do(ctx, request{method: "GET", path: "/api/v1/releases/" + id + "/gate"}, nil)
	})
	var ae *APIError
	return !errors.As(err, &ae) || ae.Code != "NOT_EVALUATED"
}

// wait asks for the gate until it decides.
func (o *checkOptions) wait(ctx context.Context, env Env, c *Client, id string) (json.RawMessage, Gate, error) {
	start := env.Now()
	lastNote := start
	failures := 0
	undecided := func(err error) error {
		if errors.Is(err, context.DeadlineExceeded) {
			return fmt.Errorf("the gate of release %s did not decide within %s; it is still evaluating. "+
				"Check it again later with: agenttwin release check --release %s", id, o.timeout, id)
		}
		return err
	}
	for {
		var raw json.RawMessage
		err := c.do(ctx, request{method: "GET", path: "/api/v1/releases/" + id + "/gate"}, &raw)
		var g Gate
		switch {
		case err == nil:
			if uerr := json.Unmarshal(raw, &g); uerr != nil {
				return nil, g, fmt.Errorf("the gate of release %s: %w", id, uerr)
			}
			if g.Decided() {
				return raw, g, nil
			}
		case !Transient(err):
			return nil, g, undecided(err)
		default:
			// Waiting is long: a few more failures in a row are allowed
			// than for a single request.
			if failures++; failures == 2*maxAttempts {
				return nil, g, err
			}
			_, _ = fmt.Fprintf(env.Stderr, "agenttwin: %v; retrying\n", err)
		}
		if err == nil {
			failures = 0
		}
		if now := env.Now(); now.Sub(lastNote) >= 30*time.Second {
			_, _ = fmt.Fprintf(env.Stderr, "agenttwin: still evaluating (%s)\n", now.Sub(start).Round(time.Second))
			lastNote = now
		}
		if serr := env.Sleep(ctx, o.pollInterval); serr != nil {
			return nil, g, undecided(serr)
		}
	}
}

// write renders the report: the text report to stdout, and the asked
// format to --output (or to stdout instead of the text report).
func (o *checkOptions) write(env Env, r reportInput) error {
	var out []byte
	var err error
	switch o.format {
	case "json":
		out, err = renderJSON(r)
	case "junit":
		out, err = renderJUnit(r, o.ci)
	default:
		out = []byte(renderText(r))
	}
	if err != nil {
		return err
	}
	if o.output == "" {
		_, err = env.Stdout.Write(out)
		return err
	}
	if o.format != "text" {
		if _, err := io.WriteString(env.Stdout, renderText(r)); err != nil {
			return err
		}
	}
	if err := os.WriteFile(o.output, out, 0o644); err != nil { //nolint:gosec // a report for CI to read
		return fmt.Errorf("--output: %w", err)
	}
	return nil
}
