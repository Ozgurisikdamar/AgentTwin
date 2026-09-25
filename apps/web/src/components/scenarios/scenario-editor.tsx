"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, Copy, Play, Save } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { SeverityBadge } from "@/components/simulations/badges";
import { sortVersions } from "@/components/simulations/new-simulation";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError, api, withQuery } from "@/lib/api";
import { type BodyOf, queryOf } from "@/lib/api/simulation";
import { formatDateTime, shortId } from "@/lib/format";
import { asScenario, getIn, newScenarioYaml, parseYaml, scenarioName, setIn } from "@/lib/scenario-yaml";
import { actorLabel } from "@/lib/simulations";
import type {
  Agent,
  AgentVersion,
  Project,
  RunResponse,
  ScenarioDetail,
  ScenarioPage,
  ScenarioSaved,
  ScenarioValidation,
  SimulationCapabilities,
  TwinDetail,
  TwinList,
} from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { ScenarioForm } from "./scenario-form";
import { ScenarioPreview } from "./scenario-preview";
import { ValidationPanel } from "./validation-panel";

const VALIDATE_DELAY_MS = 500;

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}

/** Warns before leaving the page with unsaved edits (full page loads only). */
function useUnsavedGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
}

function RunNow({
  projectId,
  agentName,
  scenario,
  blockedReason,
}: {
  projectId: string;
  agentName: string | null;
  scenario: string;
  blockedReason: string | null;
}) {
  const router = useRouter();
  const [version, setVersion] = useState("");
  const runKey = useActionKey("run-now");
  const capabilities = useQuery({
    queryKey: ["simulation-capabilities"],
    queryFn: ({ signal }) => api<SimulationCapabilities>("/simulations/capabilities", { signal }),
    staleTime: 300_000,
  });
  const agents = useQuery({
    queryKey: ["project-agents", projectId],
    queryFn: ({ signal }) => api<{ items: Agent[] }>(`/projects/${projectId}/agents`, { signal }),
    enabled: Boolean(projectId && agentName),
  });
  const agent = agents.data?.items.find((a) => a.name === agentName);
  const versions = useQuery({
    queryKey: ["agent-versions", agent?.id],
    queryFn: ({ signal }) => api<{ items: AgentVersion[] }>(`/agents/${agent!.id}/versions`, { signal }),
    enabled: Boolean(agent),
  });
  const list = sortVersions(versions.data?.items ?? []);
  const chosen = list.some((v) => v.version === version) ? version : (list[0]?.version ?? "");
  const runnable = agentName ? (capabilities.data?.agents ?? []).includes(agentName) : false;
  const start = useMutation({
    mutationFn: () =>
      api<RunResponse>("/simulations", {
        method: "POST",
        idempotencyKey: runKey.key,
        body: {
          project_id: projectId,
          agent: agentName ?? "",
          agent_version: chosen,
          scenarios: [scenario],
        } satisfies BodyOf<"startSimulation">,
      }),
    onSuccess: (res) => router.push(`/simulations/${res.run.id}`),
    onSettled: (_d, error) => runKey.settle(error),
  });
  const reason =
    blockedReason ??
    (!agentName
      ? "The scenario names no agent."
      : !capabilities.isPending && !runnable
        ? `No endpoint is configured to run ${agentName}.`
        : !agents.isPending && !agent
          ? `${agentName} is not registered in this project.`
          : null);

  return (
    <div className="space-y-2" data-testid="run-now">
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Label htmlFor="run-version">Agent version</Label>
          <Select
            id="run-version"
            value={chosen}
            onChange={(e) => setVersion(e.target.value)}
            disabled={Boolean(reason) || list.length === 0}
          >
            {list.map((v) => (
              <option key={v.id} value={v.version}>
                {v.version}
              </option>
            ))}
          </Select>
        </div>
        <Button
          variant="primary"
          size="md"
          onClick={() => start.mutate()}
          disabled={Boolean(reason) || !chosen || start.isPending}
        >
          <Play className="h-4 w-4" aria-hidden="true" />
          {start.isPending ? "Starting…" : "Run now"}
        </Button>
      </div>
      {reason ? <p className="text-xs text-slate-500">{reason}</p> : null}
      {start.isError ? <ErrorState error={start.error} /> : null}
    </div>
  );
}

export function ScenarioEditor({ scenarioId }: { scenarioId?: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const qc = useQueryClient();
  const canWrite = useCan("scenario.write");
  const canRun = useCan("simulation.run");
  const me = useMe();
  const isNew = !scenarioId;
  const versionParam = isNew ? null : searchParams.get("version");
  // Set after saving a new or renamed scenario: the editor remounts on its page.
  const savedParam = isNew ? null : searchParams.get("saved");
  const fromId = isNew ? searchParams.get("from") : null;
  const sourceId = scenarioId ?? fromId;

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const capabilities = useQuery({
    queryKey: ["simulation-capabilities"],
    queryFn: ({ signal }) => api<SimulationCapabilities>("/simulations/capabilities", { signal }),
    staleTime: 300_000,
  });
  const detail = useQuery({
    queryKey: ["scenario", sourceId, versionParam ?? "latest"],
    queryFn: ({ signal }) =>
      api<ScenarioDetail>(
        withQuery(
          `/scenarios/${sourceId}`,
          queryOf<"getScenario">({ version: versionParam ? Number(versionParam) : undefined }),
        ),
        { signal },
      ),
    enabled: Boolean(sourceId),
  });
  const projectId =
    detail.data?.scenario.project_id ?? searchParams.get("project_id") ?? projects.data?.items[0]?.id ?? "";
  const twins = useQuery({
    queryKey: ["twins", projectId],
    queryFn: ({ signal }) =>
      api<TwinList>(withQuery("/twins", queryOf<"listTwins">({ project_id: projectId })), { signal }),
    enabled: Boolean(projectId),
  });

  // The editor text is loaded once per source (scenario version or template).
  const sourceKey = isNew ? `new:${fromId ?? ""}` : `${scenarioId}@${detail.data?.version ?? ""}`;
  let initial: string | null = null;
  if (!isNew && detail.data) {
    initial = detail.data.yaml;
  } else if (isNew && fromId && detail.data) {
    initial = setIn(detail.data.yaml, ["metadata", "name"], `${detail.data.scenario.name}-copy`);
  } else if (isNew && !fromId && capabilities.data && (twins.data || twins.isError)) {
    initial = newScenarioYaml({
      name: "new-scenario",
      agent: capabilities.data.agents[0],
      twin: twins.data?.items[0]?.name,
    });
  }
  const [loaded, setLoaded] = useState<{ key: string; text: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [text, setText] = useState("");
  if (initial !== null && loaded?.key !== sourceKey) {
    setLoaded({ key: sourceKey, text: initial });
    setText(initial);
    if (savedParam && detail.data) {
      const { scenario, version } = detail.data;
      setNotice(
        searchParams.get("unchanged")
          ? `No changes: ${scenario.name} is identical to version ${version}.`
          : `Saved ${scenario.name} as version ${version}.`,
      );
    }
  }
  const dirty = loaded !== null && text !== loaded.text;
  useUnsavedGuard(dirty && canWrite);

  const parsed = useMemo(() => parseYaml(text), [text]);
  const doc = parsed.ok ? asScenario(parsed.value) : null;
  const name = parsed.ok ? scenarioName(parsed.value) : null;
  const twinName = parsed.ok ? getIn(parsed.value, ["spec", "twin"]) : null;
  const twinRow = twins.data?.items.find((t) => t.name === twinName);
  const twinDetail = useQuery({
    queryKey: ["twin", twinRow?.id],
    queryFn: ({ signal }) => api<TwinDetail>(`/twins/${twinRow!.id}`, { signal }),
    enabled: Boolean(twinRow),
    staleTime: 300_000,
  });

  // Validation follows the text (debounced); the previous answer stays visible meanwhile.
  const toValidate = useDebounced(text, VALIDATE_DELAY_MS);
  const validation = useQuery({
    queryKey: ["scenario-validation", projectId, toValidate],
    queryFn: ({ signal }) =>
      api<ScenarioValidation>("/scenarios/validate", {
        method: "POST",
        body: { project_id: projectId, yaml: toValidate } satisfies BodyOf<"validateScenario">,
        signal,
      }),
    enabled: Boolean(projectId && toValidate && parseYaml(toValidate).ok),
    staleTime: Number.POSITIVE_INFINITY,
    placeholderData: keepPreviousData,
  });
  const upToDate = toValidate === text && !validation.isPlaceholderData && !validation.isFetching;

  // Saving is by name: a name another scenario already has adds a version to that one.
  const checkName = useDebounced(name, VALIDATE_DELAY_MS);
  const otherName = checkName && (isNew || checkName !== detail.data?.scenario.name) ? checkName : null;
  const sameName = useQuery({
    queryKey: ["scenario-by-name", projectId, otherName],
    queryFn: ({ signal }) =>
      api<ScenarioPage>(
        withQuery(
          "/scenarios",
          queryOf<"listScenarios">({ project_id: projectId, name: otherName!, include_archived: "true" }),
        ),
        { signal },
      ),
    enabled: Boolean(projectId && otherName),
    staleTime: 10_000,
  });
  const taken = otherName === name ? (sameName.data?.items[0] ?? null) : null;

  // The notice was read into state; drop it from the URL so a reload does not repeat it.
  useEffect(() => {
    if (savedParam && loaded && scenarioId) router.replace(`/scenarios/${scenarioId}`, { scroll: false });
  }, [savedParam, loaded, scenarioId, router]);
  const saveKey = useActionKey("save-scenario");
  const save = useMutation({
    mutationFn: () =>
      api<ScenarioSaved>("/scenarios", {
        method: "POST",
        idempotencyKey: saveKey.key,
        body: { project_id: projectId, yaml: text } satisfies BodyOf<"saveScenario">,
      }),
    onSuccess: (res) => {
      setNotice(
        res.created
          ? `Saved ${res.scenario.name} as version ${res.version}.`
          : `No changes: identical to version ${res.version}.`,
      );
      void qc.invalidateQueries({ queryKey: ["scenario"] });
      void qc.invalidateQueries({ queryKey: ["scenarios-list"] });
      if (loaded) setLoaded({ ...loaded, text });
      if (res.scenario.id !== scenarioId) {
        router.push(`/scenarios/${res.scenario.id}?saved=1${res.created ? "" : "&unchanged=1"}`);
      } else if (versionParam) router.replace(`/scenarios/${res.scenario.id}`);
    },
    onSettled: (_d, error) => saveKey.settle(error),
  });
  const archiveKey = useActionKey("archive");
  const [confirmArchive, setConfirmArchive] = useState(false);
  const archive = useMutation({
    mutationFn: () =>
      api(`/scenarios/${scenarioId}/archive`, { method: "POST", idempotencyKey: archiveKey.key }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["scenarios-list"] });
      router.push("/scenarios");
    },
    onSettled: (_d, error) => archiveKey.settle(error),
  });

  const [tab, setTab] = useState(isNew ? "form" : "preview");

  if (detail.isError) {
    const e = detail.error;
    if (e instanceof ApiError && e.status === 404) {
      return (
        <EmptyState title="Scenario not found">
          <Link href="/scenarios" className="text-indigo-700 underline">
            Back to scenarios
          </Link>
        </EmptyState>
      );
    }
    return <ErrorState error={e} />;
  }
  if (loaded === null) {
    const failed = projects.error ?? capabilities.error;
    if (failed) return <ErrorState error={failed} />;
    if (projects.data && projects.data.items.length === 0) {
      return <EmptyState title="No project is visible to you">Scenarios belong to a project.</EmptyState>;
    }
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading scenario">
        <Skeleton className="h-10 w-96" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  const meta = detail.data && !isNew ? detail.data : null;
  const archived = meta?.scenario.archived ?? false;
  const oldVersion = meta && meta.version !== meta.scenario.latest_version;
  const readOnly = !canWrite;
  const renamed = !isNew && meta && name && name !== meta.scenario.name && !taken;
  const toolList = twinDetail.data?.twin.tools ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href="/scenarios" className="text-indigo-700 hover:underline">
            Scenarios
          </Link>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            {isNew ? "New scenario" : meta?.scenario.name}
            {meta ? <SeverityBadge severity={meta.scenario.severity} /> : null}
            {meta ? <Badge tone="brand">v{meta.version}</Badge> : null}
            {archived ? <Badge>archived</Badge> : null}
            {dirty ? (
              <Badge tone="warning" data-testid="unsaved">
                unsaved changes
              </Badge>
            ) : null}
          </span>
        }
        description={
          // An existing scenario's description is in the preview right below; repeating it here
          // only pushed the editor down.
          isNew
            ? "Describe the conversation, the tool state it starts from, the faults to inject and what must be true at the end."
            : undefined
        }
        actions={
          canWrite ? (
            <>
              {!isNew ? (
                <Link href={`/scenarios/new?from=${scenarioId}`} className={buttonVariants({ size: "sm" })}>
                  <Copy className="h-4 w-4" aria-hidden="true" />
                  Duplicate
                </Link>
              ) : null}
              {!isNew && !archived ? (
                confirmArchive ? (
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => archive.mutate()}
                    disabled={archive.isPending}
                  >
                    <Archive className="h-4 w-4" aria-hidden="true" />
                    {archive.isPending ? "Archiving…" : "Confirm archive"}
                  </Button>
                ) : (
                  <Button size="sm" onClick={() => setConfirmArchive(true)}>
                    <Archive className="h-4 w-4" aria-hidden="true" />
                    Archive
                  </Button>
                )
              ) : null}
              <Button
                size="sm"
                variant="primary"
                onClick={() => {
                  setNotice(null);
                  save.mutate();
                }}
                disabled={!parsed.ok || save.isPending || (!dirty && !isNew && !oldVersion)}
              >
                <Save className="h-4 w-4" aria-hidden="true" />
                {save.isPending ? "Saving…" : oldVersion && !dirty ? "Restore this version" : "Save"}
              </Button>
            </>
          ) : null
        }
      />
      {notice ? (
        <p
          role="status"
          className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900"
        >
          {notice}
        </p>
      ) : null}
      {save.isError ? <ErrorState error={save.error} /> : null}
      {archive.isError ? <ErrorState error={archive.error} /> : null}
      {oldVersion ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          You are viewing version {meta.version}; the latest is {meta.scenario.latest_version}.{" "}
          <Link href={`/scenarios/${scenarioId}`} className="underline">
            View the latest
          </Link>
          {canWrite ? ". Saving this content creates a new latest version." : "."}
        </p>
      ) : null}
      {taken ? (
        <p
          className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
          data-testid="name-taken"
        >
          A scenario named <strong>{taken.name}</strong> already exists
          {taken.archived ? " (archived)" : ""}: saving adds version {taken.latest_version + 1} to{" "}
          <Link href={`/scenarios/${taken.id}`} className="underline">
            that scenario
          </Link>
          {taken.archived ? " and restores it" : ""}. Choose another name to create a separate one.
        </p>
      ) : null}
      {renamed ? (
        <p className="rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-900">
          The name changed: saving creates a separate scenario named <strong>{name}</strong> and keeps{" "}
          <strong>{meta.scenario.name}</strong> as it is.
        </p>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <Tabs value={tab} onValueChange={setTab}>
            <CardHeader>
              <TabsList aria-label="Editor view">
                <TabsTrigger value="preview">Preview</TabsTrigger>
                <TabsTrigger value="form">Form</TabsTrigger>
                <TabsTrigger value="yaml">YAML</TabsTrigger>
              </TabsList>
              {readOnly ? <span className="text-xs text-slate-500">read-only for your role</span> : null}
            </CardHeader>
            <CardContent>
              <TabsContent value="preview" className="mt-0">
                {doc ? (
                  <ScenarioPreview doc={doc} />
                ) : (
                  <p className="text-sm text-slate-600">Fix the YAML to see the preview.</p>
                )}
              </TabsContent>
              <TabsContent value="form" className="mt-0">
                {parsed.ok ? (
                  <ScenarioForm
                    text={text}
                    value={parsed.value}
                    onChange={setText}
                    readOnly={readOnly}
                    nameEditable={isNew}
                    agents={capabilities.data?.agents ?? []}
                    twins={(twins.data?.items ?? []).map((t) => t.name)}
                    tools={toolList}
                    faultTypes={capabilities.data?.fault_types ?? []}
                    expectationTypes={capabilities.data?.expectation_types ?? []}
                  />
                ) : (
                  <p className="text-sm text-slate-600">
                    The form needs valid YAML: fix the syntax error in the YAML tab
                    {parsed.line ? ` (line ${parsed.line})` : ""}.
                  </p>
                )}
              </TabsContent>
              <TabsContent value="yaml" className="mt-0 space-y-2">
                <textarea
                  aria-label="Scenario YAML"
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                  readOnly={readOnly}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  className="h-[36rem] w-full resize-y rounded-md border border-slate-300 bg-slate-50 p-3 font-mono text-xs leading-5 text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-200"
                />
                <p className="text-xs text-slate-500">
                  Scenarios are stored as structured data: comments are not kept when you save. Keep annotated
                  sources in your repository and load them with the SDK or the seed.
                </p>
              </TabsContent>
            </CardContent>
          </Tabs>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Validation</CardTitle>
              <span className="text-xs text-slate-500">schema and twin checks</span>
            </CardHeader>
            <CardContent>
              <ValidationPanel
                syntax={parsed.ok ? null : { error: parsed.error, line: parsed.line }}
                result={validation.data}
                checking={!upToDate}
                error={validation.error}
              />
            </CardContent>
          </Card>

          {!isNew && canRun ? (
            <Card>
              <CardHeader>
                <CardTitle>Run now</CardTitle>
                <span className="text-xs text-slate-500">this scenario only</span>
              </CardHeader>
              <CardContent>
                <RunNow
                  projectId={projectId}
                  agentName={meta?.scenario.agent ?? null}
                  scenario={meta?.scenario.name ?? ""}
                  blockedReason={
                    archived
                      ? "Archived scenarios do not run."
                      : dirty
                        ? "Save your changes first: a run uses the saved version."
                        : oldVersion
                          ? "Runs use the latest version."
                          : null
                  }
                />
              </CardContent>
            </Card>
          ) : null}

          {meta ? (
            <Card>
              <CardHeader>
                <CardTitle>Versions</CardTitle>
                <span className="text-xs text-slate-500">{meta.versions.length}</span>
              </CardHeader>
              <CardContent>
                <ol className="space-y-1.5 text-sm" aria-label="Versions">
                  {meta.versions.map((v) => (
                    <li key={v.id} className="flex items-baseline justify-between gap-2">
                      {v.version === meta.version ? (
                        <span className="font-medium text-slate-900" aria-current="true">
                          v{v.version}
                        </span>
                      ) : (
                        <Link
                          href={
                            v.version === meta.scenario.latest_version
                              ? `/scenarios/${scenarioId}`
                              : `/scenarios/${scenarioId}?version=${v.version}`
                          }
                          className="text-indigo-700 hover:underline"
                        >
                          v{v.version}
                        </Link>
                      )}
                      <span className="truncate text-xs text-slate-500" title={v.spec_hash}>
                        {actorLabel(v.created_by, me?.user.id)} · {formatDateTime(v.created_at)}
                      </span>
                    </li>
                  ))}
                </ol>
                <p className="mt-3 text-xs text-slate-500">
                  Source {meta.scenario.source} · spec{" "}
                  <code title={meta.scenario.spec_hash}>{shortId(meta.scenario.spec_hash, 10)}</code>
                </p>
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}
