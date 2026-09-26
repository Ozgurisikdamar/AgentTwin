// Records one run and lets the process end without flush() or shutdown():
// the SDK's exit hook must export what is still queued.
const { agentTrace, configure } = await import(process.env.SDK_ENTRY);

configure({ scheduleDelayMs: 60_000 });
agentTrace({ agent: "exiting" }, (run) => run.toolCall("t", () => 1));
