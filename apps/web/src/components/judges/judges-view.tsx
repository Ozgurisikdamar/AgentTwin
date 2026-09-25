"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { CalibratedBadge } from "@/components/evaluations/badges";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KeyValue } from "@/components/ui/key-value";
import { Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import {
  type BodyOf,
  type Calibration,
  type CalibrationPage,
  type CalibrationResponse,
  type Criterion,
  type JudgeDescription,
  queryOf,
} from "@/lib/api/evaluation";
import { parseCalibrationExamples } from "@/lib/evaluations";
import { formatDateTime, formatPercent, humanize, shortId } from "@/lib/format";
import { actorLabel } from "@/lib/simulations";
import type { Project } from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";

const ACTIVE = new Set(["QUEUED", "RUNNING"]);

const STATUS_TONE: Record<Calibration["status"], BadgeTone> = {
  QUEUED: "neutral",
  RUNNING: "info",
  COMPLETED: "neutral",
  FAILED: "danger",
};

/** A starting point for the examples box: the shape one example takes. */
export const EXAMPLE_TEMPLATE = `[
  {
    "id": "refund-confirmed-1",
    "rubric": "The reply tells the customer the refund was issued.",
    "customer_message": "I want a refund for order ORD-1001.",
    "answer": "Your refund of $40 was issued for order ORD-1001.",
    "human_label": "pass"
  }
]`;

function CalibrationResult({ c }: { c: Calibration }) {
  if (c.status === "FAILED") return <span className="text-xs text-rose-800">{c.error ?? "failed"}</span>;
  if (!c.metrics) return <span className="text-xs text-slate-500">{humanize(c.status)}…</span>;
  const m = c.metrics;
  return (
    <div className="space-y-1 text-xs text-slate-700">
      <div className="flex flex-wrap items-center gap-2">
        {c.calibrated !== null ? <CalibratedBadge calibrated={c.calibrated} /> : null}
        <span>
          agreed {m.agreed} of {m.examples} ({formatPercent(m.accuracy, 0)})
        </span>
        <span>κ {m.kappa === null ? "—" : m.kappa.toFixed(2)}</span>
        {m.errors ? <span className="text-amber-900">{m.errors} could not be graded</span> : null}
      </div>
      {c.reason ? <p className="text-slate-600">{c.reason}</p> : null}
    </div>
  );
}

function Disagreements({ id }: { id: string }) {
  const detail = useQuery({
    queryKey: ["calibration", id],
    queryFn: ({ signal }) => api<CalibrationResponse>(`/judges/calibrations/${id}`, { signal }),
  });
  if (detail.isPending) return <Skeleton className="h-10 w-full" />;
  if (detail.isError) return <ErrorState error={detail.error} />;
  const rows = detail.data.calibration.disagreements ?? [];
  if (rows.length === 0) return <p className="text-xs text-slate-600">The judge agreed with every label.</p>;
  return (
    <table className="w-full text-left text-xs" data-testid="disagreements">
      <caption className="sr-only">Examples the judge labeled differently</caption>
      <thead className="text-slate-500">
        <tr>
          <th scope="col" className="py-1 font-medium">
            Example
          </th>
          <th scope="col" className="py-1 font-medium">
            Person
          </th>
          <th scope="col" className="py-1 font-medium">
            Judge
          </th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {rows.map((d) => (
          <tr key={d.id}>
            <td className="py-1 font-mono">{d.id}</td>
            <td className="py-1">{d.human}</td>
            <td className="py-1">
              {d.judge ?? "no verdict"}
              {d.score !== undefined ? ` (${d.score.toFixed(2)})` : ""}
              {d.error ? <span className="block text-rose-800">{d.error}</span> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StartCalibration({
  project,
  criteria,
  minExamples,
  onStarted,
}: {
  project: string;
  criteria: Criterion[];
  minExamples: number;
  onStarted: () => void;
}) {
  const [criterion, setCriterion] = useState<Criterion>(criteria[0] ?? "rubric");
  const [text, setText] = useState("");
  const key = useActionKey("calibrate");
  const parsed = parseCalibrationExamples(text);
  const start = useMutation({
    mutationFn: () => {
      const body: BodyOf<"startJudgeCalibration"> = {
        project_id: project,
        criterion,
        examples: parsed.examples,
      };
      return api<CalibrationResponse>("/judges/calibrations", {
        method: "POST",
        idempotencyKey: key.key,
        body,
      });
    },
    onSuccess: () => {
      setText("");
      onStarted();
    },
    onSettled: (_data, error) => key.settle(error),
  });
  const count = parsed.examples.length;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (count > 0 && !start.isPending) start.mutate();
  }

  return (
    <form
      onSubmit={submit}
      aria-label="Calibrate the judge"
      className="space-y-3"
      data-testid="calibrate-form"
    >
      <div className="max-w-xs">
        <Label htmlFor="cal-criterion">Criterion</Label>
        <Select
          id="cal-criterion"
          value={criterion}
          onChange={(e) => setCriterion(e.target.value as Criterion)}
        >
          {criteria.map((c) => (
            <option key={c} value={c}>
              {humanize(c)}
            </option>
          ))}
        </Select>
      </div>
      <div>
        <Label htmlFor="cal-examples">Labeled examples (a JSON array, or one JSON object per line)</Label>
        <textarea
          id="cal-examples"
          rows={8}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={EXAMPLE_TEMPLATE}
          spellCheck={false}
          aria-describedby="cal-examples-help"
          aria-invalid={parsed.problems.length > 0}
          className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-2 font-mono text-xs text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-200"
        />
        <div id="cal-examples-help" className="mt-1 text-xs">
          {parsed.problems.length ? (
            <ul className="list-disc pl-5 text-rose-800">
              {parsed.problems.slice(0, 8).map((p) => (
                <li key={p}>{p}</li>
              ))}
              {parsed.problems.length > 8 ? <li>and {parsed.problems.length - 8} more</li> : null}
            </ul>
          ) : (
            <p
              className={count && count < minExamples ? "text-amber-900" : "text-slate-500"}
              data-testid="example-count"
            >
              {count === 0
                ? `Paste at least ${minExamples} examples a person labeled pass or fail.`
                : `${count} ${count === 1 ? "example" : "examples"}${
                    count < minExamples
                      ? ` — a calibration needs at least ${minExamples} to count; fewer are graded but cannot calibrate the judge.`
                      : "."
                  }`}
            </p>
          )}
        </div>
      </div>
      {start.isError ? <ErrorState error={start.error} /> : null}
      <Button type="submit" variant="primary" size="sm" disabled={count === 0 || start.isPending}>
        <FlaskConical className="h-4 w-4" aria-hidden="true" />
        {start.isPending ? "Starting…" : "Calibrate"}
      </Button>
    </form>
  );
}

export function JudgesView() {
  const qc = useQueryClient();
  const me = useMe();
  const canCalibrate = useCan("review.write");
  const { params, update } = useUrlQuery();
  const [open, setOpen] = useState<string | null>(null);
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const project = params.get("project_id") || projects.data?.items[0]?.id || "";
  const judge = useQuery({
    queryKey: ["judge", project],
    queryFn: ({ signal }) =>
      api<JudgeDescription>(withQuery("/judges", queryOf<"describeJudge">({ project_id: project })), {
        signal,
      }),
    enabled: Boolean(project),
  });
  const calibrations = useQuery({
    queryKey: ["calibrations", project],
    queryFn: ({ signal }) =>
      api<CalibrationPage>(
        withQuery(
          "/judges/calibrations",
          queryOf<"listJudgeCalibrations">({ project_id: project, limit: 50 }),
        ),
        { signal },
      ),
    enabled: Boolean(project),
    refetchInterval: (q) => (q.state.data?.items.some((c) => ACTIVE.has(c.status)) ? 1_500 : false),
  });
  // A calibration that just finished changes which criteria are calibrated.
  const running = calibrations.data?.items.some((c) => ACTIVE.has(c.status)) ?? false;
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) void qc.invalidateQueries({ queryKey: ["judge", project] });
    wasRunning.current = running;
  }, [running, project, qc]);

  if (projects.isPending || (project && judge.isPending)) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading the judge">
        <Skeleton className="h-10 w-80" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  const error = projects.error ?? judge.error;
  if (error) return <ErrorState error={error} />;
  if (!judge.data) return <EmptyState title="No project to show" />;
  const j = judge.data;
  const projectList = projects.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href="/reviews" className="text-indigo-700 hover:underline">
            Reviews
          </Link>
        }
        title="Judge calibration"
        description="The judge that grades semantic expectations, measured against labels people gave. Until it passes a calibration for a criterion, its verdicts on critical expectations of that criterion go to a person."
        actions={
          projectList.length > 1 ? (
            <Select
              aria-label="Project"
              className="w-48"
              value={project}
              onChange={(e) => update({ project_id: e.target.value })}
            >
              {projectList.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          ) : null
        }
      />
      <div className="grid gap-4 xl:grid-cols-3">
        <Card data-testid="judge-identity">
          <CardHeader>
            <CardTitle>Judge</CardTitle>
          </CardHeader>
          <CardContent>
            <KeyValue
              items={[
                { label: "Provider", value: j.judge.provider },
                { label: "Model", value: j.judge.model },
                { label: "Kind", value: j.judge.kind === "llm" ? "language model" : j.judge.kind },
                { label: "Prompt", value: j.judge.prompt_version },
                {
                  label: "Prompt hash",
                  value: (
                    <code className="text-xs" title={j.judge.prompt_sha256}>
                      {shortId(j.judge.prompt_sha256, 12)}
                    </code>
                  ),
                },
              ]}
            />
            {j.judge.kind === "deterministic-fake" ? (
              <p className="mt-3 text-xs text-amber-900">
                This is the deterministic keyword judge used without a model API key: it is not a language
                model.
              </p>
            ) : null}
            <p className="mt-3 text-xs text-slate-600">
              To count, a calibration needs at least {j.requirements.min_examples} examples,{" "}
              {formatPercent(j.requirements.min_agreement, 0)} agreement and Cohen&apos;s κ of at least{" "}
              {j.requirements.min_kappa}. A new model or prompt starts uncalibrated.
            </p>
          </CardContent>
        </Card>
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Criteria</CardTitle>
            <span className="text-xs text-slate-500">the latest completed calibration counts</span>
          </CardHeader>
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Calibration of each criterion</caption>
            <tbody className="divide-y divide-slate-100">
              {j.criteria.map((c) => (
                <tr
                  key={c.criterion}
                  data-testid="criterion"
                  data-criterion={c.criterion}
                  data-calibrated={c.calibrated}
                >
                  <th scope="row" className="px-3 py-2 font-medium text-slate-900">
                    {humanize(c.criterion)}
                  </th>
                  <td className="px-3 py-2">
                    <CalibratedBadge calibrated={c.calibrated} />
                  </td>
                  <td className="px-3 py-2">
                    {c.calibration ? (
                      <CalibrationResult c={c.calibration} />
                    ) : (
                      <span className="text-xs text-slate-500">never calibrated</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      {canCalibrate ? (
        <Card>
          <CardHeader>
            <CardTitle>Calibrate</CardTitle>
            <span className="text-xs text-slate-500">
              the judge grades each example; its labels are compared with yours
            </span>
          </CardHeader>
          <CardContent>
            <StartCalibration
              project={project}
              criteria={j.criteria.map((c) => c.criterion)}
              minExamples={j.requirements.min_examples}
              onStarted={() => void qc.invalidateQueries({ queryKey: ["calibrations", project] })}
            />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Calibrations</CardTitle>
          <span className="text-xs text-slate-500">newest first</span>
        </CardHeader>
        {calibrations.isPending ? (
          <Skeleton className="m-4 h-24" />
        ) : calibrations.isError ? (
          <ErrorState error={calibrations.error} className="m-4" />
        ) : calibrations.data.items.length === 0 ? (
          <EmptyState title="No calibration yet" />
        ) : (
          <ul className="divide-y divide-slate-100">
            {calibrations.data.items.map((c) => (
              <li
                key={c.id}
                className="space-y-2 px-4 py-3"
                data-testid="calibration"
                data-status={c.status}
                data-criterion={c.criterion}
              >
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium text-slate-900">{humanize(c.criterion)}</span>
                  <Badge tone={STATUS_TONE[c.status]}>{humanize(c.status)}</Badge>
                  <span className="text-xs text-slate-500">
                    {c.example_count} examples · {actorLabel(c.requested_by, me?.user?.id)} ·{" "}
                    {formatDateTime(c.created_at)}
                    {c.judge ? ` · ${c.judge.model}` : ""}
                  </span>
                  {c.status === "COMPLETED" ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-auto h-7 px-2 text-xs"
                      aria-expanded={open === c.id}
                      onClick={() => setOpen(open === c.id ? null : c.id)}
                    >
                      {open === c.id ? "Hide disagreements" : "Disagreements"}
                    </Button>
                  ) : null}
                </div>
                <CalibrationResult c={c} />
                {open === c.id ? <Disagreements id={c.id} /> : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
