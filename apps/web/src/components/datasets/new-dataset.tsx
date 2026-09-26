"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Save } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api } from "@/lib/api";
import type { BodyOf, DatasetDetail } from "@/lib/api/evaluation";
import { parseTags } from "@/lib/evaluations";
import { NAME } from "@/lib/ids";
import type { Project } from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { ScenarioPicker } from "./scenario-picker";

export function NewDataset() {
  const router = useRouter();
  const params = useSearchParams();
  const canWrite = useCan("scenario.write");
  const [projectId, setProjectId] = useState(params.get("project_id") ?? "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tagText, setTagText] = useState("");
  const [cases, setCases] = useState<Set<string>>(new Set());
  const key = useActionKey("dataset");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const project = projectId || projects.data?.items[0]?.id || "";
  const tags = parseTags(tagText);
  const nameValid = NAME.test(name);

  const create = useMutation({
    mutationFn: () => {
      const body: BodyOf<"createDataset"> = {
        project_id: project,
        name,
        ...(description.trim() ? { description: description.trim() } : {}),
        tags: tags.tags,
        cases: [...cases].sort().map((scenario) => ({ scenario })),
      };
      return api<DatasetDetail>("/datasets", { method: "POST", idempotencyKey: key.key, body });
    },
    onSuccess: (res) => router.push(`/datasets/${res.dataset.id}`),
    onSettled: (_data, error) => key.settle(error),
  });
  const ready = Boolean(project && nameValid && tags.invalid.length === 0 && cases.size > 0);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !create.isPending) create.mutate();
  }

  if (!canWrite) {
    return (
      <EmptyState title="Your role cannot create datasets">
        Ask an engineer or an administrator of this organization, or{" "}
        <Link href="/datasets" className="text-indigo-700 underline">
          go back to the datasets
        </Link>
        .
      </EmptyState>
    );
  }

  return (
    <form className="space-y-4" onSubmit={submit} aria-label="New dataset">
      <PageHeader
        eyebrow={
          <Link href="/datasets" className="text-indigo-700 hover:underline">
            Datasets
          </Link>
        }
        title="New dataset"
        description="A named set of the project's scenarios. Every change makes a new version; evaluations pin the version they ran."
        actions={
          <Button type="submit" variant="primary" size="sm" disabled={!ready || create.isPending}>
            <Save className="h-4 w-4" aria-hidden="true" />
            {create.isPending
              ? "Creating…"
              : `Create with ${cases.size} ${cases.size === 1 ? "case" : "cases"}`}
          </Button>
        }
      />
      {projects.isError ? <ErrorState error={projects.error} /> : null}
      {create.isError ? <ErrorState error={create.error} /> : null}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Dataset</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {(projects.data?.items.length ?? 0) > 1 ? (
              <div>
                <Label htmlFor="ds-new-project">Project</Label>
                <Select
                  id="ds-new-project"
                  value={project}
                  onChange={(e) => {
                    setProjectId(e.target.value);
                    setCases(new Set());
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
              <Label htmlFor="ds-new-name">Name</Label>
              <Input
                id="ds-new-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="refund-release-gate"
                aria-invalid={Boolean(name) && !nameValid}
                aria-describedby="ds-new-name-help"
              />
              <p
                id="ds-new-name-help"
                className={`mt-1 text-xs ${name && !nameValid ? "text-rose-700" : "text-slate-500"}`}
              >
                Lowercase letters, digits, - and _; unique in the project.
              </p>
            </div>
            <div>
              <Label htmlFor="ds-new-description">Description (optional)</Label>
              <Input
                id="ds-new-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="ds-new-tags">Tags (optional)</Label>
              <Input
                id="ds-new-tags"
                value={tagText}
                onChange={(e) => setTagText(e.target.value)}
                placeholder="release, refunds"
                aria-invalid={tags.invalid.length > 0}
                aria-describedby="ds-new-tags-help"
              />
              <p
                id="ds-new-tags-help"
                className={`mt-1 text-xs ${tags.invalid.length ? "text-rose-700" : "text-slate-500"}`}
              >
                {tags.invalid.length
                  ? `Not a tag: ${tags.invalid.join(", ")}`
                  : "Separated by commas or spaces."}
              </p>
            </div>
          </CardContent>
        </Card>
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Cases</CardTitle>
            <span className="text-xs text-slate-500">scenarios of the project</span>
          </CardHeader>
          {project ? (
            <ScenarioPicker
              projectId={project}
              selected={cases}
              onChange={setCases}
              legend="Scenarios to include"
            />
          ) : null}
        </Card>
      </div>
    </form>
  );
}
