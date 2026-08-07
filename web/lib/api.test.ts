import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchEdgeLabel } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchEdgeLabel", () => {
  it("sends only the call-site sample consumed by the server", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ label: "calls helper", cached: false, disabled: false }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await fetchEdgeLabel("repo", {
      source: { file: "src/a.py", line: 1, end_line: 2, label: "a" },
      target: { file: "src/b.py", line: 3, end_line: 4, label: "b" },
      anchors: Array.from({ length: 20 }, (_, index) => ({
        file: "src/a.py",
        line: index + 1,
      })),
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const payload = JSON.parse(String(init.body));
    expect(payload.anchors).toHaveLength(3);
    expect(payload.anchors[2]).toEqual({ file: "src/a.py", line: 3 });
  });
});
