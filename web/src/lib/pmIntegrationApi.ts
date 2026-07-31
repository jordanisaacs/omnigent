// Typed client for Omnigent's read-only projection of live PM projects.
//
// PM projects are deliberately not Omnigent projects: these objects are
// ephemeral views discovered from connected hosts and must never be written
// into Omnigent's project/session metadata. The view id is therefore used only
// to address this API and to seed the new-session composer.

import { authenticatedFetch } from "./identity";

export interface PmWorktree {
  name: string;
  repo: string;
  path: string;
  status: string;
  included: boolean;
  slot_uuid?: string | null;
  branch?: string | null;
  detail?: string | null;
}

export interface PmProjectDirectory {
  name: string;
  path: string;
}

export interface PmProject {
  id: string;
  object: "pm.project";
  name: string;
  host_id: string;
  host_name: string;
  path: string;
  workspace: string;
  lease_count: number;
  worktrees: PmWorktree[];
  /** Active worktrees only, as authoritative session additional roots. */
  directories: PmProjectDirectory[];
}

interface PmProjectListResponse {
  object: "list";
  data: PmProject[];
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string };
    return body.error?.message ?? body.message ?? `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

export async function listPmProjects(): Promise<PmProject[]> {
  const res = await authenticatedFetch("/v1/integrations/pm/projects");
  if (!res.ok) throw new Error(await readError(res));
  return ((await res.json()) as PmProjectListResponse).data;
}

export async function getPmProject(viewId: string): Promise<PmProject> {
  const res = await authenticatedFetch(
    `/v1/integrations/pm/projects/${encodeURIComponent(viewId)}`,
  );
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as PmProject;
}
