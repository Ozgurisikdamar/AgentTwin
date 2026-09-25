package cli

import (
	"context"
	"fmt"
	"slices"
	"strings"
)

// doctor checks that the CLI can work: the control plane answers, the
// credential is valid, and it may check releases of the project.
func doctor(ctx context.Context, args []string, env Env) (int, error) {
	var c common
	var project string
	fs := newFlags("agenttwin doctor", env)
	c.register(fs, env)
	fs.StringVar(&project, "project", env.Getenv("AGENTTWIN_PROJECT"), "the project, by id or slug (AGENTTWIN_PROJECT)")
	if err := parse(fs, args); err != nil {
		return ExitError, err
	}
	cl, err := c.client(env)
	if err != nil {
		return ExitError, err
	}
	ok := true
	check := func(name string, err error, detail string) {
		if err != nil {
			ok = false
			_, _ = fmt.Fprintf(env.Stdout, "✗ %-12s %v\n", name, err)
			return
		}
		_, _ = fmt.Fprintf(env.Stdout, "✓ %-12s %s\n", name, detail)
	}
	err = cl.do(ctx, request{method: "GET", path: "/health/ready"}, nil)
	check("control plane", err, cl.BaseURL+" is ready")
	if err != nil {
		return ExitError, nil
	}
	var me struct {
		Principal struct {
			Actor string `json:"actor"`
			Role  string `json:"role"`
		} `json:"principal"`
		Organization struct {
			Name string `json:"name"`
		} `json:"organization"`
		Permissions []string `json:"permissions"`
	}
	err = cl.do(ctx, request{method: "GET", path: "/api/v1/me"}, &me)
	check("credential", err, fmt.Sprintf("%s (%s) in %s", me.Principal.Actor, me.Principal.Role, me.Organization.Name))
	if err != nil {
		return ExitError, nil
	}
	if !slices.Contains(me.Permissions, "release.write") {
		check("permissions", fmt.Errorf("release.write is missing (has %s); use a CI key", strings.Join(me.Permissions, ", ")), "")
	} else {
		check("permissions", nil, "can create and check releases")
	}
	if project != "" {
		id, err := projectID(ctx, cl, project)
		check("project", err, id)
	}
	if !ok {
		return ExitError, nil
	}
	return ExitPass, nil
}
