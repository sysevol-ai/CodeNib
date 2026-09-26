import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let fetchRepos: typeof import("./api")["fetchRepos"];

beforeEach(async () => {
  vi.resetModules();
  ({ fetchRepos } = await import("./api"));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const response = (id: string) => ({
  ok: true,
  json: async () => [{ id }],
});

describe("repository navigation cache", () => {
  it("shares an in-flight list and reuses it across home, Wiki, and Ask", async () => {
    let resolve!: (value: ReturnType<typeof response>) => void;
    const fetchMock = vi.fn().mockReturnValue(new Promise((done) => { resolve = done; }));
    vi.stubGlobal("fetch", fetchMock);

    const home = fetchRepos();
    const wiki = fetchRepos();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    resolve(response("repo"));
    expect(await home).toEqual(await wiki);
    expect(await fetchRepos()).toEqual([{ id: "repo" }]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("revalidates expired lists and supports an explicit refresh", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1000);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response("old"))
      .mockResolvedValueOnce(response("new"))
      .mockResolvedValueOnce(response("refreshed"));
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchRepos()).toEqual([{ id: "old" }]);
    vi.setSystemTime(60_999);
    expect(await fetchRepos()).toEqual([{ id: "old" }]);
    vi.setSystemTime(61_000);
    expect(await fetchRepos()).toEqual([{ id: "new" }]);
    expect(await fetchRepos({ refresh: true })).toEqual([{ id: "refreshed" }]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("retries failed requests instead of caching a backend outage", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValueOnce(response("recovered"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchRepos()).rejects.toThrow("Failed to load repos (503)");
    expect(await fetchRepos()).toEqual([{ id: "recovered" }]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps a newer refresh when an older request fails later", async () => {
    let reject!: (reason: Error) => void;
    const fetchMock = vi.fn()
      .mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }))
      .mockResolvedValueOnce(response("new"));
    vi.stubGlobal("fetch", fetchMock);

    const old = fetchRepos();
    const rejected = expect(old).rejects.toThrow("old connection failed");
    expect(await fetchRepos({ refresh: true })).toEqual([{ id: "new" }]);
    reject(new Error("old connection failed"));
    await rejected;
    expect(await fetchRepos()).toEqual([{ id: "new" }]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("isolates dialog cancellation from the shared navigation request", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response("shared"))
      .mockImplementationOnce((_url: string, options: RequestInit) =>
        new Promise((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(controller.signal.reason));
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await fetchRepos();
    const dialog = fetchRepos({ signal: controller.signal });
    const rejected = expect(dialog).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await rejected;
    expect(await fetchRepos()).toEqual([{ id: "shared" }]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1].signal).toBe(controller.signal);
  });

  it("does not reuse an API list for another static publication root", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response("live"))
      .mockResolvedValueOnce(response("static"));
    vi.stubGlobal("fetch", fetchMock);

    await fetchRepos();
    vi.stubGlobal("window", { __CODENIB_RUNTIME__: { mode: "static", basePath: "/preview" } });
    expect(await fetchRepos()).toEqual([{ id: "static" }]);
    expect(fetchMock.mock.calls[1][0]).toBe("/preview/data/repos.json");
  });
});
