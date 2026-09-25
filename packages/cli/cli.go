// Package cli is the agenttwin command-line tool (spec §39): it checks a
// release in CI and talks to the AgentTwin API through the control plane.
//
// Exit codes: 0 pass, 2 warn, 3 block, 4 infrastructure or configuration
// error. With --ci, a warning the project's gate policy does not fail CI
// exits 0.
package cli

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/buildinfo"
)

// Exit codes (spec §39).
const (
	ExitPass  = 0
	ExitWarn  = 2
	ExitBlock = 3
	ExitError = 4
)

// Env is what a command runs with: its output, the environment and the
// clock (tests replace them).
type Env struct {
	Stdout, Stderr io.Writer
	Stdin          io.Reader
	Getenv         func(string) string
	Now            func() time.Time
	Sleep          func(context.Context, time.Duration) error
	HTTP           *http.Client
}

// DefaultEnv is the process's environment.
func DefaultEnv() Env {
	return Env{Stdout: os.Stdout, Stderr: os.Stderr, Stdin: os.Stdin, Getenv: os.Getenv, Now: time.Now, Sleep: sleep}
}

func sleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

const usage = `agenttwin — simulate, evaluate and gate AI agent releases.

Usage:
  agenttwin release check [flags]   create a release, wait for its gate, exit with its outcome
  agenttwin agent validate FILE     validate an agent manifest
  agenttwin agent register FILE     register an agent version from its manifest
  agenttwin doctor                  check the URL, the credential and the project
  agenttwin version                 print the version

Every command takes --url, --api-key and --org; "agenttwin <command> -h"
lists the rest. The environment supplies defaults:

  AGENTTWIN_URL        the control plane (default http://localhost:8080)
  AGENTTWIN_API_KEY    a project API key (CI keys can create releases)
  AGENTTWIN_TOKEN      a session token, when no API key is set
  AGENTTWIN_ORG        the organization, for credentials in several
  AGENTTWIN_PROJECT    the project (id or slug)
  AGENTTWIN_WEB_URL    the web app, to link reports to it

Exit codes: 0 pass, 2 warn, 3 block, 4 infrastructure or configuration error.
`

// Main runs the command line args and returns the exit code.
func Main(ctx context.Context, args []string, env Env) int {
	if len(args) == 0 {
		_, _ = io.WriteString(env.Stderr, usage)
		return ExitError
	}
	var err error
	code := ExitPass
	switch args[0] {
	case "-h", "--help", "help":
		_, _ = io.WriteString(env.Stdout, usage)
		return ExitPass
	case "version", "--version":
		_, _ = fmt.Fprintf(env.Stdout, "agenttwin %s %s\n", buildinfo.Version, buildinfo.Revision())
		return ExitPass
	case "release":
		code, err = sub(ctx, args[1:], env, map[string]command{"check": releaseCheck})
	case "agent":
		code, err = sub(ctx, args[1:], env, map[string]command{"validate": agentValidate, "register": agentRegister})
	case "doctor":
		code, err = doctor(ctx, args[1:], env)
	default:
		err = usageError(fmt.Sprintf("unknown command %q", args[0]))
	}
	if errors.Is(err, flag.ErrHelp) {
		return ExitPass
	}
	if err != nil {
		report(env.Stderr, err)
		return ExitError
	}
	return code
}

// command runs a subcommand and returns its exit code.
type command func(context.Context, []string, Env) (int, error)

func sub(ctx context.Context, args []string, env Env, cmds map[string]command) (int, error) {
	if len(args) == 0 || cmds[args[0]] == nil {
		names := make([]string, 0, len(cmds))
		for n := range cmds {
			names = append(names, n)
		}
		want := strings.Join(sortedStrings(names), ", ")
		if len(args) == 0 {
			return ExitError, usageError("a subcommand is required: " + want)
		}
		return ExitError, usageError(fmt.Sprintf("unknown subcommand %q (one of %s)", args[0], want))
	}
	return cmds[args[0]](ctx, args[1:], env)
}

// usageError is a mistake in the command line.
type usageError string

func (e usageError) Error() string { return string(e) }

// reportedError is a mistake the flag package already explained.
type reportedError struct{ error }

// report explains an error on stderr, with what to do about it.
func report(w io.Writer, err error) {
	var re reportedError
	if errors.As(err, &re) {
		return
	}
	var ue usageError
	var ae *APIError
	var ne *networkError
	switch {
	case errors.As(err, &ue):
		_, _ = fmt.Fprintf(w, "agenttwin: %s\nRun \"agenttwin --help\" for usage.\n", ue)
	case errors.As(err, &ae):
		_, _ = fmt.Fprintf(w, "agenttwin: the API refused the request: %s\n", ae)
		switch ae.Status {
		case http.StatusUnauthorized:
			_, _ = io.WriteString(w, "Check AGENTTWIN_API_KEY (or --api-key): it is missing, revoked or expired.\n")
		case http.StatusForbidden:
			_, _ = io.WriteString(w, "The credential lacks the permission; a CI key (scope \"ci\") can create and check releases.\n")
		}
	case errors.As(err, &ne):
		_, _ = fmt.Fprintf(w, "agenttwin: cannot reach AgentTwin: %s\nCheck AGENTTWIN_URL (or --url) and that the control plane is running.\n", ne)
	default:
		_, _ = fmt.Fprintf(w, "agenttwin: %s\n", err)
	}
}

// common are the flags every API command takes.
type common struct {
	url, apiKey, token, org string
}

func (c *common) register(fs *flag.FlagSet, env Env) {
	fs.StringVar(&c.url, "url", orDefault(env.Getenv("AGENTTWIN_URL"), "http://localhost:8080"), "the control plane's URL (AGENTTWIN_URL)")
	fs.StringVar(&c.apiKey, "api-key", env.Getenv("AGENTTWIN_API_KEY"), "a project API key (AGENTTWIN_API_KEY)")
	fs.StringVar(&c.org, "org", env.Getenv("AGENTTWIN_ORG"), "the organization id, for credentials in several (AGENTTWIN_ORG)")
	c.token = env.Getenv("AGENTTWIN_TOKEN")
}

func (c *common) client(env Env) (*Client, error) {
	u := strings.TrimRight(strings.TrimSpace(c.url), "/")
	if !strings.HasPrefix(u, "http://") && !strings.HasPrefix(u, "https://") {
		return nil, usageError(fmt.Sprintf("--url must be an http(s) URL, got %q", c.url))
	}
	if c.apiKey == "" && c.token == "" {
		return nil, usageError("no credential: set AGENTTWIN_API_KEY (or --api-key)")
	}
	return &Client{BaseURL: u, APIKey: c.apiKey, Token: c.token, Org: c.org, HTTP: env.HTTP,
		UserAgent: "agenttwin-cli/" + buildinfo.Version}, nil
}

// newFlags makes a flag set that reports its own errors to stderr.
func newFlags(name string, env Env) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(env.Stderr)
	return fs
}

// parse parses args, turning flag errors into usage errors.
func parse(fs *flag.FlagSet, args []string) error {
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return err
		}
		return reportedError{err}
	}
	return nil
}

func orDefault(v, def string) string {
	if v != "" {
		return v
	}
	return def
}
