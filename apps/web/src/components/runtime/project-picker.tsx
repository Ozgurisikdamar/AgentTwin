"use client";

import { useQuery } from "@tanstack/react-query";
import { Label, Select } from "@/components/ui/input";
import { api } from "@/lib/api";
import type { Project } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";

/** The project a runtime page shows: the URL's, else the first visible. */
export function useProject() {
  const { params } = useUrlQuery();
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const list = projects.data?.items ?? [];
  const projectId = params.get("project_id") || list[0]?.id || "";
  return { projects, list, projectId };
}

/** A project selector, shown only when the user sees more than one. */
export function ProjectPicker({ id, list, projectId }: { id: string; list: Project[]; projectId: string }) {
  const { update } = useUrlQuery();
  if (list.length <= 1) return null;
  return (
    <div className="w-48">
      <Label htmlFor={id}>Project</Label>
      <Select id={id} value={projectId} onChange={(e) => update({ project_id: e.target.value, cursor: "" })}>
        {list.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </Select>
    </div>
  );
}
