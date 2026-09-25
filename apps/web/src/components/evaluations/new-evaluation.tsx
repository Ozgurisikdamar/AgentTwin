"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { GitCompareArrows } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useMemo, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan } from "@/components/shell/me-context";
import { SeverityBadge } from "@/components/simulations/badges";
import { sortVersions } from "@/components/simulations/new-simulation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import {
  type BodyOf,
  type DatasetDetail,
  type DatasetPage,
  type EvalRunResponse,
  queryOf as evaluationQuery,
} from "@/lib/api/evaluation";
import { queryOf as simulationQuery } from "@/lib/api/simulation";
import type { Agent, AgentVersion, Project, ScenarioPage, SimulationCapabilities } from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";

type Suite = "dataset" | "scenarios";

/** The default pair: the newest version as the candidate, the one before it as the baseline. */
export function defaultPair(versions: readonly AgentVersion[]): { baseline: string; candidate: string } {
  const sorted = sortVersions(versions);
  const candidate = sorted[0]?.version ?? "";
  return { baseline: sorted[1]?.version ?? candidate, candidate };
}

export function NewEvaluation() {
  const router = useRouter();
  const params = useSearchParams();
  const canRun = useCan("eval.run");
  const [projectId, setProjectId] = useState(params.get("project_id") ?? "");
  const [agentName, setAgentName] = useState(params.get("agent") ?? "");
  const [baseline, setBaseline] = useState(params.get("baseline") ?? "");
  const [candidate, setCandidate] = useState(params.get("candidate") ?? "");
  const [suite, setSuite] = useState<Suite>(params.get("suite") === "scenarios" ? "scenarios" : "dataset");
  const [datasetId, setDatasetId] = useState(params.get("dataset_id") ?? "");
  const [datasetVersion, setDatasetVersion] = useState("");
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [seed, setSeed] = useState("");
  const startKey = useActionKey("evaluate");

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
  const known = (v: string) => versionList.some((x) => x.version === v);
  const pair = defaultPair(versionList);
  const chosenBaseline = known(baseline) ? baseline : pair.baseline;
  const chosenCandidate = known(candidate) ? candidate : pair.candidate;

  const datasets = useQuery({
    queryKey: ["datasets", project, ""],
    queryFn: ({ signal }) =>
      api<DatasetPage>(
        withQuery("/datasets", evaluationQuery<"listDatasets">({ project_id: project, limit: 200 })),
        { signal },
      ),
    enabled: Boolean(project),
  });
  const datasetList = datasets.data?.items ?? [];
  const dataset = datasetList.find((d) => d.id === datasetId) ?? datasetList[0];
  const datasetDetail = useQuery({
    queryKey: ["dataset", dataset?.id, ""],
    queryFn: ({ signal }) => api<DatasetDetail>(`/datasets/${dataset!.id}`, { signal }),
    enabled: suite === "dataset" && Boolean(dataset),
  });
  const datasetVersions = datasetDetail.data?.versions ?? [];
  const chosenDatasetVersion = datasetVersions.some((v) => String(v.version) === datasetVersion)
    ? Number(datasetVersion)
    : undefined;
  const datasetCases =
    (chosenDatasetVersion
      ? datasetVersions.find((v) => v.version === chosenDatasetVersion)?.case_count
      : dataset?.case_count) ?? 0;

  const scenarios = useQuery({
    queryKey: ["scenarios", project, agent?.name],
    queryFn: ({ signal }) =>
      api<ScenarioPage>(
        withQuery(
          "/scenarios",
          simulationQuery<"listScenarios">({ project_id: project, agent: agent!.name, limit: 200 }),
        ),
        { signal },
      ),
    enabled: suite === "scenarios" && Boolean(project && agent),
  });
  const scenarioList = scenarios.data?.items ?? [];
  const selected = scenarioList.filter((s) => !excluded.has(s.name));

  const start = useMutation({
    mutationFn: () => {
      const body: BodyOf<"startEvalRun"> = {
        project_id: project,
        agent: agent!.name,
        baseline_version: chosenBaseline,
        candidate_version: chosenCandidate,
        ...(suite === "dataset"
          ? {
              dataset_id: dataset!.id,
              ...(chosenDatasetVersion ? { dataset_version: chosenDatasetVersion } : {}),
            }
          : excluded.size
            ? { scenarios: selected.map((s) => s.name) }
            : {}),
        ...(seed.trim() ? { seed: Number(seed) } : {}),
      };
      return api<EvalRunResponse>("/eval-runs", { method: "POST", idempotencyKey: startKey.key, body });
    },
    onSuccess: (res) => router.push(`/evaluations/${res.run.id}`),
    onSettled: (_data, error) => startKey.settle(error),
  });

  const seedValid = !seed.trim() || (/^\d+$/.test(seed.trim()) && Number(seed) <= 4_294_967_295);
  const suiteReady = suite === "dataset" ? Boolean(dataset) && datasetCases > 0 : selected.length > 0;
  const ready = Boolean(
    project &&
    agent &&
    runnable.has(agent.name) &&
    chosenBaseline &&
    chosenCandidate &&
    suiteReady &&
    seedValid,
  );
  const caseCount = suite === "dataset" ? datasetCases : selected.length;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !start.isPending) start.mutate();
  }

  if (!canRun) {
    return (
      <EmptyState title="Your role cannot start evaluations">
        Ask an engineer or an administrator of this organization, or{" "}
        <Link href="/evaluations" className="text-indigo-700 underline">
          go back to the evaluations
        </Link>
        .
      </EmptyState>
    );
  }

  const loading = projects.isPending || capabilities.isPending || (Boolean(project) && agents.isPending);
  const loadError =
    projects.error ??
    capabilities.error ??
    agents.error ??
    versions.error ??
    datasets.error ??
    datasetDetail.error ??
    scenarios.error;

  return (
    <form className="space-y-4" onSubmit={submit} aria-label="New evaluation">
      <PageHeader
        eyebrow={
          <Link href="/evaluations" className="text-indigo-700 hover:underline">
            Evaluations
          </Link>
        }
        title="New evaluation"
        description="Run a baseline and a candidate version on the same pinned scenarios, with the same seed and the same twins, then compare every case."
        actions={
          <Button type="submit" variant="primary" size="sm" disabled={!ready || start.isPending}>
            <GitCompareArrows className="h-4 w-4" aria-hidden="true" />
            {start.isPending
              ? "Starting…"
              : `Compare on ${caseCount} ${caseCount === 1 ? "scenario" : "scenarios"}`}
          </Button>
        }
      />
      {loadError ? <ErrorState error={loadError} /> : null}
      {start.isError ? <ErrorState error={start.error} /> : null}
      {loading ? (
        <Skeleton className="h-40 w-full" />
      ) : (
        <div className="grid gap-4 xl:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>Versions</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {(projects.data?.items.length ?? 0) > 1 ? (
                <div>
                  <Label htmlFor="eval-new-project">Project</Label>
                  <Select
                    id="eval-new-project"
                    value={project}
                    onChange={(e) => {
                      setProjectId(e.target.value);
                      setAgentName("");
                      setBaseline("");
                      setCandidate("");
                      setDatasetId("");
                      setExcluded(new Set());
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
                <Label htmlFor="eval-new-agent">Agent</Label>
                <Select
                  id="eval-new-agent"
                  value={agent?.name ?? ""}
                  onChange={(e) => {
                    setAgentName(e.target.value);
                    setBaseline("");
                    setCandidate("");
                    setExcluded(new Set());
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
                <Label htmlFor="eval-new-baseline">Baseline version</Label>
                <Select
                  id="eval-new-baseline"
                  value={chosenBaseline}
                  onChange={(e) => setBaseline(e.target.value)}
                >
                  {versionList.map((v) => (
                    <option key={v.id} value={v.version}>
                      {v.version}
                      {v.model_name ? ` · ${v.model_name}` : ""}
                    </option>
                  ))}
                </Select>
              </div>
              <div>
                <Label htmlFor="eval-new-candidate">Candidate version</Label>
                <Select
                  id="eval-new-candidate"
                  value={chosenCandidate}
                  onChange={(e) => setCandidate(e.target.value)}
                  aria-describedby="eval-new-candidate-help"
                >
                  {versionList.map((v) => (
                    <option key={v.id} value={v.version}>
                      {v.version}
                      {v.model_name ? ` · ${v.model_name}` : ""}
                    </option>
                  ))}
                </Select>
                {chosenBaseline && chosenBaseline === chosenCandidate ? (
                  <p id="eval-new-candidate-help" className="mt-1 text-xs text-slate-500">
                    The same version on both sides shows how repeatable it is.
                  </p>
                ) : null}
              </div>
              <div>
                <Label htmlFor="eval-new-seed">Seed (optional)</Label>
                <Input
                  id="eval-new-seed"
                  inputMode="numeric"
                  placeholder="random"
                  value={seed}
                  onChange={(e) => setSeed(e.target.value)}
                  aria-invalid={!seedValid}
                  aria-describedby="eval-new-seed-help"
                />
                <p
                  id="eval-new-seed-help"
                  className={`mt-1 text-xs ${seedValid ? "text-slate-500" : "text-rose-700"}`}
                >
                  {seedValid
                    ? "Both sides run with this seed, so the same faults hit both."
                    : "A seed is a whole number from 0 to 4294967295."}
                </p>
              </div>
            </CardContent>
          </Card>
          <Card className="xl:col-span-2">
            <CardHeader>
              <CardTitle>Suite</CardTitle>
              <div role="radiogroup" aria-label="Suite" className="flex gap-3 text-sm">
                <label className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name="suite"
                    className="accent-indigo-600"
                    checked={suite === "dataset"}
                    onChange={() => setSuite("dataset")}
                  />
                  From a dataset
                </label>
                <label className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name="suite"
                    className="accent-indigo-600"
                    checked={suite === "scenarios"}
                    onChange={() => setSuite("scenarios")}
                  />
                  Pick scenarios
                </label>
              </div>
            </CardHeader>
            {suite === "dataset" ? (
              datasets.isPending ? (
                <Skeleton className="m-4 h-24" />
              ) : datasetList.length === 0 ? (
                <EmptyState title="This project has no datasets yet">
                  Pick scenarios instead, or{" "}
                  <Link href="/datasets" className="text-indigo-700 underline">
                    create a dataset
                  </Link>
                  .
                </EmptyState>
              ) : (
                <CardContent className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="eval-new-dataset">Dataset</Label>
                    <Select
                      id="eval-new-dataset"
                      value={dataset?.id ?? ""}
                      onChange={(e) => {
                        setDatasetId(e.target.value);
                        setDatasetVersion("");
                      }}
                    >
                      {datasetList.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name} ({d.case_count} {d.case_count === 1 ? "case" : "cases"})
                        </option>
                      ))}
                    </Select>
                    {dataset?.description ? (
                      <p className="mt-1 text-xs text-slate-600">{dataset.description}</p>
                    ) : null}
                  </div>
                  <div>
                    <Label htmlFor="eval-new-dataset-version">Version</Label>
                    <Select
                      id="eval-new-dataset-version"
                      value={chosenDatasetVersion ? String(chosenDatasetVersion) : ""}
                      onChange={(e) => setDatasetVersion(e.target.value)}
                    >
                      <option value="">Latest (v{dataset?.latest_version})</option>
                      {datasetVersions.map((v) => (
                        <option key={v.version} value={String(v.version)}>
                          v{v.version} · {v.case_count} {v.case_count === 1 ? "case" : "cases"}
                          {v.note ? ` · ${v.note}` : ""}
                        </option>
                      ))}
                    </Select>
                    <p className="mt-1 text-xs text-slate-500">
                      The version is pinned: later edits to the dataset do not change this run.
                    </p>
                  </div>
                </CardContent>
              )
            ) : scenarios.isPending && agent ? (
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
                <legend className="sr-only">Scenarios to compare</legend>
                <div className="flex gap-2 px-4 py-2 text-xs">
                  <Button size="sm" variant="ghost" onClick={() => setExcluded(new Set())}>
                    Select all
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setExcluded(new Set(scenarioList.map((s) => s.name)))}
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
                      checked={!excluded.has(s.name)}
                      onChange={(e) => {
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
