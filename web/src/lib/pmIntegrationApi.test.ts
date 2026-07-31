import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getPmProject, listPmProjects } from "./pmIntegrationApi";

function response(body: unknown, ok = true): Response {
  return {
    ok,
    status: ok ? 200 : 404,
    statusText: ok ? "OK" : "Not Found",
    json: async () => body,
  } as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

describe("PM integration API", () => {
  it("lists live PM project views", async () => {
    fetchMock.mockResolvedValueOnce(response({ object: "list", data: [{ id: "view_1" }] }));
    await expect(listPmProjects()).resolves.toEqual([{ id: "view_1" }]);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/integrations/pm/projects");
  });

  it("loads a URL-encoded PM project view id", async () => {
    fetchMock.mockResolvedValueOnce(response({ id: "view / 1", object: "pm.project" }));
    await getPmProject("view / 1");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/integrations/pm/projects/view%20%2F%201");
  });

  it("surfaces structured API errors", async () => {
    fetchMock.mockResolvedValueOnce(
      response({ error: { message: "PM project not found" } }, false),
    );
    await expect(getPmProject("missing")).rejects.toThrow("PM project not found");
  });
});
