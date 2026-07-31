import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { Conversation, ConversationsPage } from "./useConversations";
import { authenticatedFetch } from "@/lib/identity";
import { getPmProject, listPmProjects, type PmProject } from "@/lib/pmIntegrationApi";

export const PM_PROJECTS_QUERY_KEY = ["pm-projects"] as const;
export const PM_PROJECT_SESSIONS_QUERY_KEY = ["pm-project-sessions"] as const;
const PM_PROJECTS_REFETCH_INTERVAL_MS = 30_000;

/** Live PM project views across the caller's connected, PM-capable hosts. */
export function usePmProjects() {
  return useQuery<PmProject[]>({
    queryKey: PM_PROJECTS_QUERY_KEY,
    queryFn: listPmProjects,
    staleTime: 30_000,
    // PM can change outside Omnigent (project create/delete, worktree
    // attach/detach). Poll the cheap discovery projection so the virtual
    // sidebar converges without requiring a host reconnect or window focus.
    refetchInterval: PM_PROJECTS_REFETCH_INTERVAL_MS,
  });
}

/** Resolve one live PM project view for a project-scoped new-session visit. */
export function usePmProject(viewId: string) {
  return useQuery<PmProject>({
    queryKey: [...PM_PROJECTS_QUERY_KEY, viewId],
    queryFn: () => getPmProject(viewId),
    enabled: viewId !== "",
    staleTime: 30_000,
  });
}

async function fetchPmProjectSessionsPage(
  project: PmProject,
  after?: string,
  limit = 20,
): Promise<ConversationsPage> {
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: String(limit),
    host_id: project.host_id,
    workspace: project.workspace,
  });
  if (after) params.set("after", after);
  const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as ConversationsPage;
}

/**
 * Sessions whose placement exactly matches a live PM project root.
 *
 * Membership is computed by the server from `(host_id, workspace)` on every
 * request. No PM view id or project membership is stored on the session.
 */
export function usePmProjectSessions(project: PmProject, enabled: boolean) {
  return useInfiniteQuery<ConversationsPage, Error, { pages: ConversationsPage[] }>({
    queryKey: [...PM_PROJECT_SESSIONS_QUERY_KEY, project.id, project.host_id, project.workspace],
    queryFn: ({ pageParam }) =>
      fetchPmProjectSessionsPage(project, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.last_id ?? undefined) : undefined,
    enabled,
  });
}

// Keep the row type visible to consumers without making them import the
// native project hook module solely for a type.
export type PmProjectSession = Conversation;
