package cli

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// maxManifestBytes bounds a manifest file (the API accepts 1 MiB).
const maxManifestBytes = 1 << 20

// readManifest reads a manifest file and names its media type.
func readManifest(path string) ([]byte, string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, "", usageError(err.Error())
	}
	if info.Size() > maxManifestBytes {
		return nil, "", usageError(fmt.Sprintf("%s is larger than 1 MiB", path))
	}
	raw, err := os.ReadFile(path) //nolint:gosec // the file the user named
	if err != nil {
		return nil, "", usageError(err.Error())
	}
	ct := "application/yaml"
	if strings.EqualFold(filepath.Ext(path), ".json") {
		ct = "application/json"
	}
	return raw, ct, nil
}

// agentValidate checks a manifest without registering it.
func agentValidate(ctx context.Context, args []string, env Env) (int, error) {
	var c common
	fs := newFlags("agenttwin agent validate", env)
	c.register(fs, env)
	if err := parse(fs, args); err != nil {
		return ExitError, err
	}
	if fs.NArg() != 1 {
		return ExitError, usageError("agent validate takes one manifest file")
	}
	raw, ct, err := readManifest(fs.Arg(0))
	if err != nil {
		return ExitError, err
	}
	cl, err := c.client(env)
	if err != nil {
		return ExitError, err
	}
	var res struct {
		Name           string `json:"name"`
		Version        string `json:"version"`
		ManifestSHA256 string `json:"manifest_sha256"`
	}
	if err := cl.do(ctx, request{method: "POST", path: "/api/v1/manifests/validate", raw: raw, contentType: ct}, &res); err != nil {
		return ExitError, err
	}
	_, _ = fmt.Fprintf(env.Stdout, "✓ %s@%s is valid (manifest sha256 %s)\n", res.Name, res.Version, res.ManifestSHA256)
	return ExitPass, nil
}

// agentRegister registers an agent version from its manifest; registering
// the same content again is not an error.
func agentRegister(ctx context.Context, args []string, env Env) (int, error) {
	var c common
	var project, commit string
	fs := newFlags("agenttwin agent register", env)
	c.register(fs, env)
	fs.StringVar(&project, "project", env.Getenv("AGENTTWIN_PROJECT"), "the project, by id or slug (AGENTTWIN_PROJECT)")
	fs.StringVar(&commit, "commit", "", "the git commit the manifest was built from")
	if err := parse(fs, args); err != nil {
		return ExitError, err
	}
	if fs.NArg() != 1 {
		return ExitError, usageError("agent register takes one manifest file")
	}
	if project == "" {
		return ExitError, usageError("the project is required: --project (or AGENTTWIN_PROJECT)")
	}
	commit = strings.ToLower(strings.TrimSpace(commit))
	if commit != "" && !commitPattern.MatchString(commit) {
		return ExitError, usageError(fmt.Sprintf("--commit must be a git commit, got %q", commit))
	}
	raw, ct, err := readManifest(fs.Arg(0))
	if err != nil {
		return ExitError, err
	}
	cl, err := c.client(env)
	if err != nil {
		return ExitError, err
	}
	pid, err := projectID(ctx, cl, project)
	if err != nil {
		return ExitError, err
	}
	path := "/api/v1/projects/" + pid + "/agent-manifests"
	if commit != "" {
		path += "?commit_sha=" + commit
	}
	var res struct {
		Agent struct {
			Name string `json:"name"`
		} `json:"agent"`
		Version struct {
			Version string `json:"version"`
			ID      string `json:"id"`
		} `json:"version"`
		Created bool `json:"created"`
	}
	if err := cl.do(ctx, request{method: "POST", path: path, raw: raw, contentType: ct}, &res); err != nil {
		return ExitError, err
	}
	state := "registered"
	if !res.Created {
		state = "already registered with this content"
	}
	_, _ = fmt.Fprintf(env.Stdout, "✓ %s@%s %s (version %s)\n", res.Agent.Name, res.Version.Version, state, res.Version.ID)
	return ExitPass, nil
}
