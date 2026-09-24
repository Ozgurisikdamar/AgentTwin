// Package buildinfo carries the version stamped into binaries at build time:
//
//	go build -ldflags "-X github.com/Ozgurisikdamar/AgentTwin/packages/gokit/buildinfo.Version=1.2.3 \
//	  -X github.com/Ozgurisikdamar/AgentTwin/packages/gokit/buildinfo.Commit=abc1234"
//
// Unstamped builds report "dev" and the VCS revision recorded by the Go
// toolchain when available.
package buildinfo

import "runtime/debug"

// Set by -ldflags at build time.
var (
	Version = "dev"
	Commit  = ""
)

// Revision returns the stamped commit, falling back to the VCS revision the
// Go toolchain embedded (empty when neither is known).
func Revision() string {
	if Commit != "" {
		return Commit
	}
	if bi, ok := debug.ReadBuildInfo(); ok {
		for _, s := range bi.Settings {
			if s.Key == "vcs.revision" {
				return s.Value
			}
		}
	}
	return ""
}
