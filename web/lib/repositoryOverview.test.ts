// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from "vitest";
import { OpenRouterTrialSession } from "./openrouterTrial";
import {
  overviewInput,
  validateOverview,
  type OverviewInput,
} from "./repositoryOverview";
import type { RepositoryPreview } from "./githubPreview";

const KEY = "fake-preview-inference-key";
const input: OverviewInput = {
  repository: "owner/repo",
  commit: "a".repeat(40),
  description: "Example",
  paths: ["src/main.py"],
  sources: [
    {
      id: 0,
      path: "src/main.py",
      content: "def main():\n    return 1\n",
      endLine: 3,
      url: `https://github.com/owner/repo/blob/${"a".repeat(40)}/src/main.py`,
    },
  ],
};
const result = {
  summary: "A small program.",
  components: [
    {
      title: "Entry point",
      description: "Returns a value.",
      evidence: { source: 0, start_line: 1, end_line: 2 },
    },
  ],
  connections: [],
};
const sessions: OpenRouterTrialSession[] = [];
afterEach(() => {
  sessions.splice(0).forEach((s) => s.disconnect());
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
const reply = (value: unknown) => new Response(JSON.stringify(value));
function fixture(value: unknown = result, cost: number | null = 0.01) {
  const fetch = vi.fn(async (url: string, init: RequestInit) => {
    expect(new URL(url).origin).toBe("https://openrouter.ai");
    expect(init.headers).toHaveProperty("Authorization", `Bearer ${KEY}`);
    if (url.endsWith("/key"))
      return reply({
        data: { is_management_key: false, limit_remaining: 0.5 },
      });
    expect(url).toBe("https://openrouter.ai/api/v1/chat/completions");
    const body = JSON.parse(String(init.body));
    expect(body.model).toBe("anthropic/claude-sonnet-4.6");
    expect(body.max_tokens).toBe(1800);
    expect(body.messages[1].content).not.toContain(KEY);
    return reply({
      model: body.model,
      usage: cost === null ? {} : { cost },
      choices: [
        { finish_reason: "stop", message: { content: JSON.stringify(value) } },
      ],
    });
  });
  vi.stubGlobal("fetch", fetch);
  const session = new OpenRouterTrialSession();
  sessions.push(session);
  return { fetch, session };
}

describe("bounded repository overview", () => {
  it("bounds file excerpts at complete lines and keeps citation IDs contiguous", () => {
    const snapshot = {
      repository: { slug: input.repository },
      commit: input.commit,
      description: "x".repeat(2000),
      entries: Array.from({ length: 200 }, (_, i) => ({ path: `src/${i}.py` })),
    } as RepositoryPreview;
    const files = [
      "x".repeat(6000),
      ...Array(6).fill("line\n".repeat(1000)),
    ].map((content, i) => ({
      path: `${i}.py`,
      url: "https://github.com/o/r",
      content,
      lineCount: 1000,
    }));
    const bounded = overviewInput(snapshot, files);
    expect(bounded.description.length).toBe(1500);
    expect(bounded.paths).toHaveLength(80);
    expect(bounded.sources).toHaveLength(4);
    expect(bounded.sources.map((s) => s.id)).toEqual([0, 1, 2, 3]);
    expect(bounded.sources[0].endLine).toBe(160);
    expect(bounded.sources.every((s) => s.content.length <= 5000)).toBe(true);
  });
  it.each([
    { source: 1, start_line: 1, end_line: 2 },
    { source: 0, start_line: 0, end_line: 2 },
    { source: 0, start_line: 1, end_line: 4 },
    { source: 0, start_line: 2, end_line: 1 },
  ])("rejects ungrounded line ranges %o", (evidence) => {
    expect(() =>
      validateOverview(
        { ...result, components: [{ ...result.components[0], evidence }] },
        input,
      ),
    ).toThrow("valid source references");
  });
  it("rejects connections to nonexistent components", () => {
    expect(() =>
      validateOverview(
        {
          ...result,
          connections: [
            {
              from: 0,
              to: 5,
              label: "fake",
              evidence: result.components[0].evidence,
            },
          ],
        },
        input,
      ),
    ).toThrow("valid source references");
  });
  it("uses an existing key only at OpenRouter, makes one billed call, and clears it", async () => {
    const { fetch, session } = fixture();
    const account = await session.useExistingKey(KEY);
    expect(account.settingsUrl).not.toContain(KEY);
    expect(account.remaining).toBe(0.5);
    expect(JSON.stringify(session)).not.toContain(KEY);
    expect(await session.explainRepository(input)).toEqual(result);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(session.usage().reportedCost).toBe(0.01);
    session.disconnect();
    await expect(session.explainRepository(input)).rejects.toThrow(
      "Connect OpenRouter",
    );
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("records a charge for invalid model references without retrying", async () => {
    const { fetch, session } = fixture({
      ...result,
      components: [
        {
          ...result.components[0],
          evidence: { source: 99, start_line: 1, end_line: 2 },
        },
      ],
    });
    await session.useExistingKey(KEY);
    await expect(session.explainRepository(input)).rejects.toThrow(
      "valid source references",
    );
    expect(session.usage().reportedCost).toBe(0.01);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("stops reuse on missing usage and rejects empty input before billing", async () => {
    const { fetch, session } = fixture(result, null);
    await session.useExistingKey(KEY);
    await expect(
      session.explainRepository({ ...input, sources: [] }),
    ).rejects.toThrow("bounded");
    expect(fetch).toHaveBeenCalledTimes(1);
    await expect(session.explainRepository(input)).rejects.toThrow(
      "report cost",
    );
    await expect(session.explainRepository(input)).rejects.toThrow(
      "unknown cost",
    );
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("refuses management keys", async () => {
    const { fetch, session } = fixture();
    fetch.mockResolvedValue(reply({ data: { is_management_key: true } }));
    await expect(session.useExistingKey(KEY)).rejects.toThrow(
      "normal inference key",
    );
    expect(session.connected()).toBe(false);
  });
  it("cannot publish an existing key after disconnect during metadata verification", async () => {
    const { fetch, session } = fixture();
    let finish!: (response: Response) => void;
    fetch.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const attempt = session.useExistingKey(KEY);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    session.disconnect();
    finish(reply({ data: { is_management_key: false } }));
    await expect(attempt).rejects.toThrow(/cancelled/i);
    expect(session.connected()).toBe(false);
  });
  it("serializes charged calls and discards a response after cancellation", async () => {
    const { fetch, session } = fixture();
    await session.useExistingKey(KEY);
    let finish!: (response: Response) => void;
    fetch.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const first = session.explainRepository(input);
    await expect(session.explainRepository(input)).rejects.toThrow(
      "already running",
    );
    session.cancelQuery();
    finish(
      reply({
        model: "anthropic/claude-sonnet-4.6",
        usage: { cost: 0.01 },
        choices: [
          {
            finish_reason: "stop",
            message: { content: JSON.stringify(result) },
          },
        ],
      }),
    );
    await expect(first).rejects.toThrow(/cancelled/i);
    expect(session.usage().unknownCost).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
