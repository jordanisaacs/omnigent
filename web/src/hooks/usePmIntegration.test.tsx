import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PmProject } from "@/lib/pmIntegrationApi";
import { usePmProjectSessions } from "./usePmIntegration";

const project: PmProject = {
  id: "view_1",
  object: "pm.project",
  name: "demo",
  host_id: "host / 1",
  host_name: "Laptop",
  path: "/home/alice/.projects/demo",
  workspace: "/home/alice/.projects/demo",
  lease_count: 0,
  worktrees: [],
  directories: [],
};

const fetchMock = vi.fn();

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("usePmProjectSessions", () => {
  it("uses the exact host and canonical project root filters", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ data: [], first_id: null, last_id: null, has_more: false }),
    } as Response);

    const { result } = renderHook(() => usePmProjectSessions(project, true), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const url = new URL(fetchMock.mock.calls[0][0] as string, "http://omnigent.test");
    expect(url.pathname).toBe("/v1/sessions");
    expect(url.searchParams.get("host_id")).toBe(project.host_id);
    expect(url.searchParams.get("workspace")).toBe(project.workspace);
    expect(url.searchParams.has("project")).toBe(false);
  });

  it("does not fetch sessions for a collapsed PM folder", async () => {
    const { result } = renderHook(() => usePmProjectSessions(project, false), { wrapper });
    await Promise.resolve();
    expect(result.current.fetchStatus).toBe("idle");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
