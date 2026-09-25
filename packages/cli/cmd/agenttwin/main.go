// Command agenttwin is the AgentTwin CLI (spec §39).
package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"github.com/Ozgurisikdamar/AgentTwin/packages/cli"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := cli.Main(ctx, os.Args[1:], cli.DefaultEnv())
	stop()
	os.Exit(code)
}
