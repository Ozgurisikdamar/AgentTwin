// Load test of the paths spec §65 names, at a modest local concurrency
// (spec §57): the trace list, the trace ingestion path, a release summary,
// a graph query and a simulation submission, all at once.
//
// Run it through `make load-test` (scripts/load_test.py), which starts it on
// the compose network, then checks what k6 cannot see: that every trace it
// sent was stored, that the simulation runs it queued all ran, and that the
// event consumers kept up.
//
// It writes into the demo workspace, visibly apart: traces of environment
// `loadtest` (session `load-<run id>`), simulation runs of refund-happy-path.
// It reads the rest through the demo API key, which is rate limited per
// principal (RATE_LIMIT_RPS, 50/s by default): the default rates stay under
// that, so a 429 is a finding, not noise.
import http from "k6/http";
import { check, fail, sleep } from "k6";
import exec from "k6/execution";
import { Counter, Trend } from "k6/metrics";

const API = (__ENV.AGENTTWIN_API_URL || "http://control-plane:8080") + "/api/v1";
const OTLP = (__ENV.AGENTTWIN_OTLP_URL || "http://otel-collector:4318") + "/v1/traces";
const KEY = __ENV.AGENTTWIN_API_KEY || "";
const PROJECT = __ENV.AGENTTWIN_PROJECT || "support";
const RUN_ID = __ENV.LOAD_RUN_ID || "0000000000000000";
const DURATION = __ENV.LOAD_DURATION || "60s";
const SCALE = Number(__ENV.LOAD_SCALE || "1");
// The agent version the traces claim and the one the simulations run; both
// exist in the demo workspace, so the load adds no component to the graph.
const TRACE_VERSION = "1.2.4";
const SIM_VERSION = "1.3.1";
const AGENT = "support-refund-agent";
const rate = (perSecond) => Math.max(1, Math.round(perSecond * SCALE));
// Traces per second sent to the collector; the collector is not behind the
// API's rate limit, so this can go well past the other rates.
const INGEST_RATE = Number(__ENV.LOAD_INGEST_RATE || rate(10));
// About one trace a second is followed until the API shows it, chosen at
// random: a fixed pick (every tenth) would be sent at the same moment of the
// collector's one-second batching cycle each time, and would measure that
// moment instead of the spread.
const TRACK_P = 1 / INGEST_RATE;
const VISIBLE_TIMEOUT_MS = 30000;

if (!/^[0-9a-f]{16}$/.test(RUN_ID)) {
  throw new Error("LOAD_RUN_ID must be 16 lowercase hex digits");
}

function arrival(fn, perSecond, vus) {
  return {
    executor: "constant-arrival-rate",
    exec: fn,
    rate: perSecond,
    timeUnit: "1s",
    duration: DURATION,
    preAllocatedVUs: vus,
    maxVUs: vus * 4,
  };
}

// Ordinary reads and writes answer within 300 ms at the 95th percentile
// (spec §57). Change impact is not an ordinary read: it walks the graph and
// asks the simulation service which scenarios the change needs.
const P95_MS = Number(__ENV.LOAD_P95_MS || "300");

export const options = {
  scenarios: {
    trace_list: arrival("traceList", rate(10), 10),
    trace_ingest: arrival("traceIngest", INGEST_RATE, Math.max(10, Math.ceil(INGEST_RATE / 2))),
    release_summary: arrival("releaseSummary", rate(6), 10),
    graph_query: arrival("graphQuery", rate(6), 10),
    simulation_submit: arrival("simulationSubmit", rate(1), 5),
  },
  thresholds: {
    "http_req_duration{name:traces_list}": [`p(95)<${P95_MS}`],
    "http_req_duration{name:traces_list_next}": [`p(95)<${P95_MS}`],
    "http_req_duration{name:otlp_export}": [`p(95)<${P95_MS}`],
    "http_req_duration{name:release_detail}": [`p(95)<${P95_MS}`],
    "http_req_duration{name:release_gate}": [`p(95)<${P95_MS}`],
    "http_req_duration{name:graph_view}": [`p(95)<${P95_MS}`],
    "http_req_duration{name:graph_focus}": [`p(95)<${P95_MS}`],
    "http_req_duration{name:change_impact}": ["p(95)<1000"],
    "http_req_duration{name:simulation_submit}": [`p(95)<${P95_MS}`],
    // From the export leaving the "agent" to the API showing the trace:
    // the collector's batching (1 s) plus storage.
    trace_visible_ms: ["p(95)<5000"],
    trace_not_visible: ["count==0"],
    http_req_failed: ["rate==0"],
    rate_limited: ["count==0"],
    checks: ["rate==1"],
    dropped_iterations: ["count==0"],
  },
  summaryTrendStats: ["min", "med", "avg", "p(90)", "p(95)", "p(99)", "max"],
  setupTimeout: "60s",
};
// Every path is exercised (this also gives the summary a count per path).
for (const name of [
  "traces_list",
  "traces_list_next",
  "otlp_export",
  "trace_poll",
  "release_detail",
  "release_gate",
  "graph_view",
  "graph_focus",
  "change_impact",
  "simulation_submit",
]) {
  options.thresholds[`http_reqs{name:${name}}`] = ["count>0"];
}

const traceVisible = new Trend("trace_visible_ms", true);
const traceNotVisible = new Counter("trace_not_visible");
const rateLimited = new Counter("rate_limited");
const tracesAccepted = new Counter("traces_accepted");
const simulationsQueued = new Counter("simulations_queued");

const headers = { "X-AgentTwin-Api-Key": KEY, Accept: "application/json" };

function get(path, name, expected) {
  const params = { headers, tags: { name } };
  if (expected) params.responseCallback = http.expectedStatuses(...expected);
  const res = http.get(API + path, params);
  if (res.status === 429) rateLimited.add(1);
  return res;
}

function json(res) {
  try {
    return res.json();
  } catch (_) {
    return null;
  }
}

function ok(res, what) {
  return check(res, { [`${what} 200`]: (r) => r.status === 200 });
}

// ------------------------------------------------------------------ setup

export function setup() {
  if (!KEY) fail("AGENTTWIN_API_KEY is not set");
  const projects = json(get("/projects", "setup"));
  const project = ((projects && projects.items) || []).find((p) => p.slug === PROJECT);
  if (!project) fail(`project ${PROJECT} not found (run make seed)`);
  const q = `project_id=${project.id}`;
  const ids = (res) => ((json(res) || {}).items || []).map((x) => x.id);
  const releases = ids(get(`/releases?${q}`, "setup")).slice(0, 5);
  const tools = ids(get(`/graph/components?${q}&kind=TOOL`, "setup"));
  const changeSets = ids(get(`/projects/${project.id}/change-sets`, "setup")).slice(0, 3);
  if (!releases.length || !tools.length || !changeSets.length) {
    fail("the demo workspace has no releases, tools or change sets (run make seed)");
  }
  return { project: project.id, releases, tools, changeSets };
}

const pick = (xs) => xs[Math.floor(Math.random() * xs.length)];

// ------------------------------------------------------------- trace list

export function traceList(data) {
  // What the traces page asks for: the first page, sometimes filtered, and
  // sometimes the next page.
  const filters = ["", `&agent=${AGENT}`, "&status=ERROR", "&source=simulation", "&environment=production"];
  const res = get(`/traces?project_id=${data.project}&limit=50${pick(filters)}`, "traces_list");
  if (!ok(res, "trace list")) return;
  const next = (json(res) || {}).next_cursor;
  if (next && Math.random() < 0.3) {
    ok(get(`/traces?project_id=${data.project}&limit=50&cursor=${next}`, "traces_list_next"), "next page");
  }
}

// --------------------------------------------------------- trace ingestion

const hex = (n) => {
  let s = "";
  for (let i = 0; i < n; i++) s += Math.floor(Math.random() * 16).toString(16);
  return s;
};
const str = (key, value) => ({ key, value: { stringValue: value } });
const int = (key, value) => ({ key, value: { intValue: String(value) } });

// One run of the refund agent as the Python SDK records it: the agent span,
// two model calls and two read-only tool calls, metadata only.
function agentRun(traceId, session) {
  const t0 = Date.now();
  const at = (ms) => `${t0 + ms}000000`;
  const root = hex(16);
  const span = (name, parent, from, to, attributes) => ({
    traceId,
    spanId: parent === null ? root : hex(16),
    parentSpanId: parent === null ? undefined : root,
    name,
    kind: 1,
    startTimeUnixNano: at(from),
    endTimeUnixNano: at(to),
    attributes,
    status: { code: 1 },
  });
  const chat = (from, to, output) =>
    span("chat scripted-planner-v1", root, from, to, [
      str("gen_ai.operation.name", "chat"),
      str("agenttwin.span.kind", "model"),
      str("gen_ai.provider.name", "scripted"),
      str("gen_ai.request.model", "scripted-planner-v1"),
      str("gen_ai.response.model", "scripted-planner-v1"),
      int("gen_ai.usage.input_tokens", 400 + Math.floor(Math.random() * 200)),
      int("gen_ai.usage.output_tokens", output),
    ]);
  const tool = (name, from, to) =>
    span(`execute_tool ${name}`, root, from, to, [
      str("gen_ai.operation.name", "execute_tool"),
      str("agenttwin.span.kind", "tool"),
      str("gen_ai.tool.name", name),
      str("agenttwin.tool.risk", "READ"),
      str("agenttwin.tool.args_hash", hex(64)),
      str("agenttwin.tool.result_status", "ok"),
    ]);
  return {
    resourceSpans: [
      {
        resource: {
          attributes: [
            str("service.name", AGENT),
            str("agenttwin.environment", "loadtest"),
            str("agenttwin.source", "production"),
            str("agenttwin.agent.name", AGENT),
            str("agenttwin.agent.version", TRACE_VERSION),
            str("agenttwin.sdk.name", "agenttwin-load-test"),
          ],
        },
        scopeSpans: [
          {
            scope: { name: "agenttwin", version: "0.1.0" },
            spans: [
              span(`invoke_agent ${AGENT}`, null, 0, 1200, [
                str("gen_ai.operation.name", "invoke_agent"),
                str("agenttwin.span.kind", "agent"),
                str("agenttwin.session.id", session),
              ]),
              chat(10, 300, 40),
              tool("lookup_order", 310, 420),
              tool("get_refund_policy", 430, 510),
              chat(520, 1150, 120),
            ],
          },
        ],
      },
    ],
  };
}

export function traceIngest() {
  const n = exec.scenario.iterationInTest;
  // The run id and the iteration: unique per run, and easy to recognize.
  const traceId = RUN_ID + n.toString(16).padStart(16, "0");
  const started = Date.now();
  const res = http.post(OTLP, JSON.stringify(agentRun(traceId, `load-${RUN_ID}`)), {
    headers: { "Content-Type": "application/json", "X-AgentTwin-Api-Key": KEY },
    tags: { name: "otlp_export" },
  });
  if (!check(res, { "export accepted": (r) => r.status === 200 })) return;
  tracesAccepted.add(1);
  if (Math.random() >= TRACK_P) return;
  // Follow this one until the API shows it; a 404 meanwhile is expected.
  while (Date.now() - started < VISIBLE_TIMEOUT_MS) {
    sleep(0.25);
    const poll = get(`/traces/${traceId}`, "trace_poll", [200, 404]);
    if (poll.status === 200) {
      traceVisible.add(Date.now() - started);
      return;
    }
    if (poll.status !== 404) {
      check(poll, { "trace poll 200 or 404": () => false });
      return;
    }
  }
  traceNotVisible.add(1);
}

// -------------------------------------------------------- release summary

export function releaseSummary(data) {
  // The release page loads the release and its gate side by side.
  const id = pick(data.releases);
  const [detail, gate] = http.batch([
    ["GET", `${API}/releases/${id}`, null, { headers, tags: { name: "release_detail" } }],
    ["GET", `${API}/releases/${id}/gate`, null, { headers, tags: { name: "release_gate" } }],
  ]);
  for (const r of [detail, gate]) if (r.status === 429) rateLimited.add(1);
  ok(detail, "release");
  ok(gate, "gate");
}

// ------------------------------------------------------------ graph query

export function graphQuery(data) {
  const p = `project_id=${data.project}`;
  const roll = Math.random();
  if (roll < 0.45) {
    // The graph page: every agent's latest version, two hops out.
    ok(get(`/graph?${p}`, "graph_view"), "graph view");
  } else if (roll < 0.9) {
    // Everything a tool reaches and is reached by, as far as the API walks.
    ok(get(`/graph?${p}&focus=${pick(data.tools)}&depth=4&limit=500`, "graph_focus"), "graph focus");
  } else {
    ok(get(`/change-sets/${pick(data.changeSets)}/impact`, "change_impact"), "change impact");
  }
}

// ---------------------------------------------------- simulation submission

export function simulationSubmit(data) {
  const n = exec.scenario.iterationInTest;
  const res = http.post(
    `${API}/simulations`,
    JSON.stringify({
      project_id: data.project,
      agent: AGENT,
      agent_version: SIM_VERSION,
      scenarios: ["refund-happy-path"],
    }),
    {
      headers: {
        ...headers,
        "Content-Type": "application/json",
        "Idempotency-Key": `load-${RUN_ID}-${n}`,
      },
      tags: { name: "simulation_submit" },
    },
  );
  if (res.status === 429) rateLimited.add(1);
  if (check(res, { "simulation queued": (r) => r.status === 202 })) simulationsQueued.add(1);
}

// ---------------------------------------------------------------- summary

export function handleSummary(data) {
  // The wrapper reads this file; it prints the report.
  return { "/out/summary.json": JSON.stringify(data, null, 1) };
}
