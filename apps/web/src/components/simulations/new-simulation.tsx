"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useMemo, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ApiError, api, withQuery } from "@/lib/api";
import { UUID } from "@/lib/ids";
import { type BodyOf, queryOf } from "@/lib/api/simulation";
import type {
  Agent,
  AgentVersion,
  Project,
  RunResponse,
  ScenarioPage,
  SimulationCapabilities,
} from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { SeverityBadge } from "./badges";

/** Newest semantic version first; unparseable versions sort last by text. */
export function sortVersions(versions: readonly AgentVersion[]): AgentVersion[] {
  const key = (v: string) => v.split(".").map((p) => Number.parseInt(p, 10));
  return [...versions].sort((a, b) => {
    const ka = key(a.version);
    const kb = key(b.version);
    for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
      const x = ka[i] ?? 0;
      const y = kb[i] ?? 0;
      if (Number.isNaN(x) || Number.isNaN(y)) return b.version.localeCompare(a.version);
      if (x !== y) return y - x;
    }
    return 0;
  });
}

function problemsOf(error: unknown): string[] {
  if (!(error instanceof ApiError) || !error.details) return [];
  const d = error.details as { problems?: unknown; scenarios?: unknown };
  const out: string[] = [];
  if (Array.isArray(d.problems)) out.push(...d.problems.map(String));
  if (d.scenarios && typeof d.scenarios === "object") {
    for (const [name, problems] of Object.entries(d.scenarios as Record<string, unknown>)) {
      for (const p of Array.isArray(problems) ? problems : [problems]) out.push(`${name}: ${String(p)}`);
    }
  }
  return out;
}

/**
 * The scenario names a link preselects (`?scenarios=a,b`, from a change
 * set's impact); null when the link names none, so every scenario is picked.
 */
export function preselectedScenarios(raw: string | null): Set<string> | null {
  const names = (raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return names.length ? new Set(names) : null;
}

export function NewSimulation() {
  const router = useRouter();
  const params = useSearchParams();
  const canRun = useCan("simulation.run");
  const [projectId, setProjectId] = useState(params.get("project_id") ?? "");
  const [agentName, setAgentName] = useState(params.get("agent") ?? "");
  const [version, setVersion] = useState(params.get("version") ?? "");
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  // Picked by a link: only these (until the user picks all or none).
  const [only, setOnly] = useState<Set<string> | null>(() => preselectedScenarios(params.get("scenarios")));
  const changeSetId = UUID.test(params.get("change_set") ?? "")
    ? params.get("change_set")!.toLowerCase()
    : "";
  const [seed, setSeed] = useState("");
  const startKey = useActionKey("simulate");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const project = projectId || projects.data?.items[0]?.id || "";
  const capabilities = useQuery({
    queryKey: ["simulation-capabilities"],
    queryFn: ({ signal }) => api<SimulationCapabilities>("/simulations/capabilities", { signal }),
    staleTime: 300_000,
  });
  const agents = useQuery({
    queryKey: ["project-agents", project],
    queryFn: ({ signal }) => api<{ items: Agent[] }>(`/projects/${project}/agents`, { signal }),
    enabled: Boolean(project),
  });
  const runnable = useMemo(() => new Set(capabilities.data?.agents ?? []), [capabilities.data]);
  const agentList = agents.data?.items ?? [];
  const agent = agentList.find((a) => a.name === agentName) ?? agentList.find((a) => runnable.has(a.name));
  const versions = useQuery({
    queryKey: ["agent-versions", agent?.id],
    queryFn: ({ signal }) => api<{ items: AgentVersion[] }>(`/agents/${agent!.id}/versions`, { signal }),
    enabled: Boolean(agent),
  });
  const versionList = sortVersions(versions.data?.items ?? []);
  const chosenVersion = versionList.some((v) => v.version === version)
    ? version
    : (versionList[0]?.version ?? "");
  const scenarios = useQuery({
    queryKey: ["scenarios", project, agent?.name],
    queryFn: ({ signal }) =>
      api<ScenarioPage>(
        withQuery(
          "/scenarios",
          queryOf<"listScenarios">({ project_id: project, agent: agent!.name, limit: 200 }),
        ),
        { signal },
      ),
    enabled: Boolean(project && agent),
  });
  const scenarioList = scenarios.data?.items ?? [];
  const isPicked = (name: string) => (only ? only.has(name) : true) && !excluded.has(name);
  const selected = scenarioList.filter((s) => isPicked(s.name));
  const missing = only ? [...only].filter((n) => !scenarioList.some((s) => s.name === n)) : [];

  const start = useMutation({
    mutationFn: () =>
      api<RunResponse>("/simulations", {
        method: "POST",
        idempotencyKey: startKey.key,
        body: {
          project_id: project,
          agent: agent!.name,
          agent_version: chosenVersion,
          // Everything selected: let the service pick every scenario of the agent.
          ...(only || excluded.size ? { scenarios: selected.map((s) => s.name) } : {}),
          ...(seed.trim() ? { seed: Number(seed) } : {}),
        } satisfies BodyOf<"startSimulation">,
      }),
    onSuccess: (res) => router.push(`/simulations/${res.run.id}`),
    onSettled: (_data, error) => startKey.settle(error),
  });

  const seedValid = !seed.trim() || (/^\d+$/.test(seed.trim()) && Number(seed) <= 4_294_967_295);
  const ready = Boolean(
    project && agent && runnable.has(agent.name) && chosenVersion && selected.length && seedValid,
  );

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !start.isPending) start.mutate();
  }

  if (!canRun) {
    return (
      <EmptyState title="Your role cannot start simulations">
        Ask an engineer or an administrator of this organization, or{" "}
        <Link href="/simulations" className="text-indigo-700 underline">
          go back to the simulations
        </Link>
        .
      </EmptyState>
    );
  }

  const loading = projects.isPending || capabilities.isPending || (Boolean(project) && agents.isPending);
  const loadError = projects.error ?? capabilities.error ?? agents.error ?? versions.error ?? scenarios.error;

  return (
    <form className="space-y-4" onSubmit={submit} aria-label="New simulation">
      <PageHeader
        eyebrow={
          <Link href="/simulations" className="text-indigo-700 hover:underline">
            Simulations
          </Link>
        }
        title="New simulation"
        description="Run one agent version against its scenarios. Each scenario gets a fresh copy of its tool twin, so nothing real is touched."
        actions={
          <Button type="submit" variant="primary" size="sm" disabled={!ready || start.isPending}>
            <Play className="h-4 w-4" aria-hidden="true" />
            {start.isPending
              ? "Starting…"
              : `Run ${selected.length} ${selected.length === 1 ? "scenario" : "scenarios"}`}
          </Button>
        }
      />
      {loadError ? <ErrorState error={loadError} /> : null}
      {start.isError ? (
        <div className="space-y-1">
          <ErrorState error={start.error} />
          {problemsOf(start.error).length ? (
            <ul className="list-disc pl-6 text-xs text-rose-800">
              {problemsOf(start.error).map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {loading ? (
        <Skeleton className="h-40 w-full" />
      ) : (
        <div className="grid gap-4 xl:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>Agent</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {(projects.data?.items.length ?? 0) > 1 ? (
                <div>
                  <Label htmlFor="new-project">Project</Label>
                  <Select
                    id="new-project"
                    value={project}
                    onChange={(e) => {
                      setProjectId(e.target.value);
                      setAgentName("");
                      setVersion("");
                      setExcluded(new Set());
                      setOnly(null);
                    }}
                  >
                    {projects.data?.items.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </Select>
                </div>
              ) : null}
              <div>
                <Label htmlFor="new-agent">Agent</Label>
                <Select
                  id="new-agent"
                  value={agent?.name ?? ""}
                  onChange={(e) => {
                    setAgentName(e.target.value);
                    setVersion("");
                    setExcluded(new Set());
                    setOnly(null);
                  }}
                >
                  {agentList.length === 0 ? <option value="">No agents registered</option> : null}
                  {agentList.map((a) => (
                    <option key={a.id} value={a.name} disabled={!runnable.has(a.name)}>
                      {a.name}
                      {runnable.has(a.name) ? "" : " (no endpoint configured)"}
                    </option>
                  ))}
                </Select>
              </div>
              <div>
                <Label htmlFor="new-version">Version</Label>
                <Select id="new-version" value={chosenVersion} onChange={(e) => setVersion(e.target.value)}>
                  {versionList.map((v) => (
                    <option key={v.id} value={v.version}>
                      {v.version}
                      {v.model_name ? ` · ${v.model_name}` : ""}
                    </option>
                  ))}
                </Select>
              </div>
              <div>
                <Label htmlFor="new-seed">Seed (optional)</Label>
                <Input
                  id="new-seed"
                  inputMode="numeric"
                  placeholder="random"
                  value={seed}
                  onChange={(e) => setSeed(e.target.value)}
                  aria-invalid={!seedValid}
                  aria-describedby="new-seed-help"
                />
                <p
                  id="new-seed-help"
                  className={`mt-1 text-xs ${seedValid ? "text-slate-500" : "text-rose-700"}`}
                >
                  {seedValid
                    ? "The same seed reproduces the same faults and twin behavior."
                    : "A seed is a whole number from 0 to 4294967295."}
                </p>
              </div>
            </CardContent>
          </Card>
          <Card className="xl:col-span-2">
            <CardHeader>
              <CardTitle>Scenarios</CardTitle>
              <span className="text-xs text-slate-500">
                {selected.length} of {scenarioList.length} selected
              </span>
            </CardHeader>
            {only ? (
              <div
                className="space-y-1 border-b border-slate-100 px-4 py-2 text-xs text-slate-600"
                data-testid="preselected-note"
              >
                <p>
                  Preselected: the scenarios{" "}
                  {changeSetId ? (
                    <Link href={`/changes/${changeSetId}`} className="text-indigo-700 underline">
                      this change
                    </Link>
                  ) : (
                    "a change"
                  )}{" "}
                  requires.
                </p>
                {missing.length && scenarios.isSuccess ? (
                  <p className="text-amber-800">Not in this agent&apos;s scenarios: {missing.join(", ")}.</p>
                ) : null}
              </div>
            ) : null}
            {scenarios.isPending && agent ? (
              <Skeleton className="m-4 h-24" />
            ) : scenarioList.length === 0 ? (
              <EmptyState title="This agent has no scenarios yet">
                <Link href="/scenarios/new" className="text-indigo-700 underline">
                  Write the first one
                </Link>
                .
              </EmptyState>
            ) : (
              <fieldset className="divide-y divide-slate-100">
                <legend className="sr-only">Scenarios to run</legend>
                <div className="flex gap-2 px-4 py-2 text-xs">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setOnly(null);
                      setExcluded(new Set());
                    }}
                  >
                    Select all
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setOnly(null);
                      setExcluded(new Set(scenarioList.map((s) => s.name)));
                    }}
                  >
                    Select none
                  </Button>
                </div>
                {scenarioList.map((s) => (
                  <label
                    key={s.id}
                    className="flex cursor-pointer items-start gap-3 px-4 py-2 hover:bg-slate-50"
                  >
                    <input
                      type="checkbox"
                      className="mt-1 h-4 w-4 accent-indigo-600"
                      checked={isPicked(s.name)}
                      onChange={(e) => {
                        if (only) {
                          const next = new Set(only);
                          if (e.target.checked) next.add(s.name);
                          else next.delete(s.name);
                          setOnly(next);
                          return;
                        }
                        const next = new Set(excluded);
                        if (e.target.checked) next.delete(s.name);
                        else next.add(s.name);
                        setExcluded(next);
                      }}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-slate-900">{s.name}</span>
                        <SeverityBadge severity={s.severity} />
                      </span>
                      {s.description ? (
                        <span className="mt-0.5 block text-xs text-slate-600">{s.description}</span>
                      ) : null}
                    </span>
                  </label>
                ))}
              </fieldset>
            )}
          </Card>
        </div>
      )}
    </form>
  );
}
