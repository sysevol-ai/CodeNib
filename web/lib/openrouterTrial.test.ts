// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  loadTrialContext,
  OpenRouterAuthorizationError,
  OpenRouterTrialSession,
  trialBase,
  type TrialContext,
} from "./openrouterTrial";

const KEY = "test-inference-credential";
const COMMIT = "a".repeat(40);
const FINGERPRINT = `sha256-v2:${"b".repeat(64)}`;
const CRITERIA = ["unrelated", "terminology", "supporting", "direct"];
const sessions: OpenRouterTrialSession[] = [];
afterEach(() => {
  for (const session of sessions.splice(0)) session.disconnect();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function fixture(
  options: {
    count?: number;
    planningCost?: number | null;
    failure?: boolean;
    invalidScore?: boolean;
    malformedPlan?: boolean;
    metadata?: Record<string, unknown> | null;
    metadataFailure?: boolean;
    exchangeFailure?: boolean;
  } = {},
) {
  const calls: {
    url: string;
    init: RequestInit;
    body: Record<string, unknown> | null;
  }[] = [];
  const context: TrialContext = {
    base: "https://source.example",
    repoId: "requests",
    repository: "psf/requests",
    commit: COMMIT,
    fingerprint: FINGERPRINT,
    payload: { issue: "", source_file_count: 1 },
    protocol: {
      planner_model: "anthropic/claude-sonnet-4.6",
      reranker_model: "typesafe/jev-1.13",
      planner_system: "Plan bounded searches",
      planner_schema: { type: "object" },
      relevance_criteria: CRITERIA,
    },
  };
  const fetch = vi.fn(async (url: string, init: RequestInit) => {
    const body = init.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, init, body });
    const reply = (value: unknown) =>
      new Response(JSON.stringify(value), { status: 200 });
    if (url.endsWith("/auth/keys"))
      return options.exchangeFailure
        ? new Response(KEY, { status: 502 })
        : reply({ key: KEY });
    if (url.endsWith("/api/v1/key")) {
      if (options.metadataFailure) return new Response(KEY, { status: 502 });
      return reply({
        data:
          options.metadata === undefined
            ? { is_management_key: false, limit_remaining: 2 }
            : options.metadata,
      });
    }
    if (url.endsWith("/chat/completions")) {
      if (options.failure) return new Response(KEY, { status: 502 });
      return reply({
        model: context.protocol.planner_model,
        usage:
          options.planningCost === null
            ? {}
            : { cost: options.planningCost ?? 0.01 },
        choices: [
          {
            finish_reason: "stop",
            message: {
              content: options.malformedPlan
                ? KEY
                : JSON.stringify({
                    actions: [
                      {
                        pattern: "retry",
                        glob: "**/*.py",
                        case_sensitive: false,
                      },
                    ],
                  }),
            },
          },
        ],
      });
    }
    if (url.endsWith("/candidates"))
      return reply({
        repo_id: context.repoId,
        repository: context.repository,
        commit: COMMIT,
        source_fingerprint: FINGERPRINT,
        candidates: Array.from({ length: options.count ?? 11 }, (_, i) => ({
          id: `node_${i}`,
          name: `retry_${i}`,
          file: "src/service.py",
          content: "source metadata\ndef retry():\n    return True",
          source: "def retry():\n    return True",
          start_line: 1,
          end_line: 2,
          url: "javascript:ignored()",
        })),
      });
    if (url.endsWith("/decisions")) {
      const questions = body.questions as Record<
        string,
        { criteria: string[] }
      >;
      return reply({
        model: "typesafe/jev-1.13-20260917",
        usage: { cost: 0.001 },
        answers: Object.fromEntries(
          Object.entries(questions).map(([id, question]) => {
            const score = id === "node_10" ? 3 : 0;
            return [
              id,
              {
                type: "score",
                score: options.invalidScore ? 4 : score,
                confidence: 1,
                probabilities: {
                  "0": 1 - score / 3,
                  "1": 0,
                  "2": 0,
                  "3": score / 3,
                },
                legend: Object.fromEntries(
                  question.criteria.map((value, i) => [String(i), value]),
                ),
              },
            ];
          }),
        ),
      });
    }
    if (url.endsWith("/trial/repos/requests"))
      return reply({
        repo_id: context.repoId,
        repository: context.repository,
        commit: COMMIT,
        source_fingerprint: FINGERPRINT,
        payload: context.payload,
        protocol: context.protocol,
      });
    throw new Error(`Unexpected URL ${KEY}`);
  });
  vi.stubGlobal("fetch", fetch);
  const session = new OpenRouterTrialSession();
  sessions.push(session);
  return { context, calls, fetch, session };
}

async function connect(session: OpenRouterTrialSession) {
  const attempt = await session.beginLogin(
    "https://demo.example/openrouter-callback.html",
  );
  const account = await session.acceptGrant(attempt.nonce, "one-use-code");
  return { attempt, account };
}

describe("browser-owned OpenRouter trial", () => {
  it("uses S256, consumes the grant once and never serializes the key", async () => {
    const { session, calls } = fixture();
    const { attempt, account } = await connect(session);
    expect(calls).toHaveLength(2);
    expect(
      calls.every((call) => call.url.startsWith("https://openrouter.ai/")),
    ).toBe(true);
    const verifier = String(calls[0].body?.code_verifier);
    const digest = new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)),
    );
    const challenge = btoa(String.fromCharCode(...digest))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
    const url = new URL(attempt.url);
    expect(url.searchParams.get("code_challenge")).toBe(challenge);
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(
      new URL(String(url.searchParams.get("callback_url"))).searchParams.get(
        "attempt",
      ),
    ).toBe(attempt.nonce);
    expect(JSON.stringify(session)).toBe("{}");
    expect(JSON.stringify(account)).not.toContain(KEY);
    expect(account.settingsUrl).toMatch(
      /^https:\/\/openrouter.ai\/keys\/[a-f0-9]{64}$/,
    );
    expect(session.connected()).toBe(true);
    await expect(
      session.acceptGrant(attempt.nonce, "one-use-code"),
    ).rejects.toThrow("consumed");
    expect(calls).toHaveLength(2);
  });

  it("rejects nonce mismatch, denial/cancellation and expiry before exchange", async () => {
    vi.useFakeTimers();
    const { session, calls } = fixture();
    const pending = await session.beginLogin(
      "https://demo.example/openrouter-callback.html",
    );
    await expect(session.acceptGrant("wrong", "code")).rejects.toThrow(
      "expired",
    );
    session.disconnect();
    await expect(session.acceptGrant(pending.nonce, "code")).rejects.toThrow(
      "consumed",
    );
    const expired = await session.beginLogin(
      "https://demo.example/openrouter-callback.html",
    );
    vi.advanceTimersByTime(300001);
    await expect(session.acceptGrant(expired.nonce, "code")).rejects.toThrow(
      "expired",
    );
    expect(calls).toEqual([]);
  });

  it.each([
    null,
    {},
    { is_management_key: true },
    { is_management_key: null },
    { is_management_key: "false" },
    { is_management_key: 0 },
    { is_management_key: false, is_provisioning_key: true },
    { is_management_key: false, is_provisioning_key: null },
    { is_management_key: false, is_provisioning_key: "false" },
  ])(
    "requires verified inference metadata and retains safe revocation guidance: %j",
    async (metadata) => {
      const { session, context, calls } = fixture({ metadata });
      const error = await connect(session).catch((reason: unknown) => reason);
      expect(error).toBeInstanceOf(OpenRouterAuthorizationError);
      expect((error as OpenRouterAuthorizationError).settingsUrl).toMatch(
        /^https:\/\/openrouter.ai\/keys\/[a-f0-9]{64}$/,
      );
      expect(String(error)).toContain("review or revoke");
      expect(String(error)).not.toContain(KEY);
      expect(JSON.stringify(error)).not.toContain(KEY);
      expect(session.connected()).toBe(false);
      await expect(session.retrieve(context, "retry")).rejects.toThrow(
        "Connect OpenRouter",
      );
      expect(calls).toHaveLength(2);
    },
  );

  it("reports an issued key after metadata transport failure without echoing its body", async () => {
    const { session, calls } = fixture({ metadataFailure: true });
    const error = await connect(session).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(OpenRouterAuthorizationError);
    expect(String(error)).toContain("HTTP 502");
    expect(String(error)).not.toContain(KEY);
    expect(session.connected()).toBe(false);
    expect(calls).toHaveLength(2);
  });

  it("offers the provider key list when the exchange outcome is unknown", async () => {
    const { session, calls } = fixture({ exchangeFailure: true });
    const error = await connect(session).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(OpenRouterAuthorizationError);
    expect((error as OpenRouterAuthorizationError).settingsUrl).toBe(
      "https://openrouter.ai/settings/keys",
    );
    expect(String(error)).not.toContain(KEY);
    expect(session.connected()).toBe(false);
    expect(calls).toHaveLength(1);
  });

  it("retains revocation guidance when cancelled during metadata verification", async () => {
    const { session, fetch } = fixture();
    const original = fetch.getMockImplementation()!;
    let started!: () => void;
    const metadataStarted = new Promise<void>((resolve) => {
      started = resolve;
    });
    fetch.mockImplementation(async (url, init) => {
      if (!url.endsWith("/api/v1/key")) return original(url, init);
      started();
      return new Promise<Response>((_resolve, reject) => {
        init.signal!.addEventListener("abort", () => reject(new Error(KEY)));
      });
    });
    const pending = connect(session).catch((reason: unknown) => reason);
    await metadataStarted;
    session.disconnect();
    const error = await pending;
    expect(error).toBeInstanceOf(OpenRouterAuthorizationError);
    expect((error as OpenRouterAuthorizationError).settingsUrl).toMatch(
      /^https:\/\/openrouter.ai\/keys\/[a-f0-9]{64}$/,
    );
    expect(String(error)).not.toContain(KEY);
    expect(session.connected()).toBe(false);
  });

  it("validates published context and keeps provider credentials off the source service", async () => {
    const { session, context, calls } = fixture();
    const published = await loadTrialContext(
      context.base,
      context.repoId,
      context.repository,
      context.commit,
      new AbortController().signal,
    );
    await connect(session);
    const results = await session.retrieve(
      published,
      "Where is retry handled?",
    );
    expect(results).toHaveLength(5);
    expect(results[0].id).toBe("node_10");
    expect(results[0].url).toBe(
      `https://github.com/psf/requests/blob/${COMMIT}/src/service.py#L1-L2`,
    );
    expect(results[0].source).toBe("def retry():\n    return True");
    const sourceCalls = calls.filter((call) =>
      call.url.startsWith(context.base),
    );
    expect(sourceCalls).toHaveLength(2);
    expect(JSON.stringify(sourceCalls)).not.toContain(KEY);
    expect(new Headers(sourceCalls[1].init.headers).has("authorization")).toBe(
      false,
    );
    expect(Object.keys(sourceCalls[1].body ?? {}).sort()).toEqual([
      "plan",
      "source_fingerprint",
    ]);
    for (const call of calls) {
      expect(call.init.credentials).toBe("omit");
      expect(call.init.redirect).toBe("error");
      expect(call.init.referrerPolicy).toBe("no-referrer");
    }
    const batches = calls.filter((call) => call.url.endsWith("/decisions"));
    expect(
      batches.map((call) => Object.keys(call.body?.questions as object).length),
    ).toEqual([10, 1]);
    expect(session.usage().reportedCost).toBeCloseTo(0.012);
    expect(session.usage().unknownCost).toBe(false);
  });

  it("stops after a reported-cost limit without reranking or automatic retries", async () => {
    const { session, context, calls } = fixture({ planningCost: 0.11 });
    await connect(session);
    await expect(session.retrieve(context, "retry")).rejects.toThrow(
      "limit reached",
    );
    expect(
      calls.filter((call) => call.url.endsWith("/chat/completions")),
    ).toHaveLength(1);
    expect(
      calls.filter((call) => call.url.endsWith("/decisions")),
    ).toHaveLength(0);
    expect(session.usage().reportedCost).toBe(0.11);
  });

  it("retains unknown charge status and does not retry missing usage", async () => {
    const { session, context, calls } = fixture({ planningCost: null });
    await connect(session);
    await expect(session.retrieve(context, "retry")).rejects.toThrow(
      "did not report cost",
    );
    const count = calls.length;
    await expect(session.retrieve(context, "retry")).rejects.toThrow(
      "unknown cost",
    );
    expect(calls).toHaveLength(count);
    expect(session.usage().unknownCost).toBe(true);
  });

  it("does not display provider error bodies or invalid planner content", async () => {
    for (const options of [{ failure: true }, { malformedPlan: true }]) {
      const { session, context } = fixture(options);
      await connect(session);
      try {
        await session.retrieve(context, "retry");
        throw new Error("Expected failure");
      } catch (error) {
        expect(String(error)).not.toContain(KEY);
        expect(String(error)).not.toContain("Expected failure");
      }
    }
  });

  it("refuses malformed Jev scores before publishing any results", async () => {
    const { session, context, calls } = fixture({ invalidScore: true });
    await connect(session);
    await expect(session.retrieve(context, "retry")).rejects.toThrow(
      "invalid score",
    );
    expect(
      calls.filter((call) => call.url.endsWith("/decisions")),
    ).toHaveLength(1);
  });

  it("removes the credential at expiry and on disconnect", async () => {
    vi.useFakeTimers();
    const { session, context, calls } = fixture();
    await connect(session);
    vi.advanceTimersByTime(600001);
    expect(session.connected()).toBe(false);
    await expect(session.retrieve(context, "retry")).rejects.toThrow(
      "Connect OpenRouter",
    );
    expect(calls).toHaveLength(2);
    await connect(session);
    session.disconnect();
    expect(session.connected()).toBe(false);
    await expect(session.retrieve(context, "retry")).rejects.toThrow(
      "Connect OpenRouter",
    );
  });

  it("aborts an in-flight query on disconnect without starting later stages", async () => {
    const { session, context, fetch, calls } = fixture();
    await connect(session);
    let reached!: () => void;
    const started = new Promise<void>((resolve) => {
      reached = resolve;
    });
    fetch.mockImplementationOnce(async (_url, init) => {
      reached();
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new Error(KEY)), {
          once: true,
        });
      });
    });
    const pending = session.retrieve(context, "retry");
    await started;
    session.disconnect();
    await expect(pending).rejects.toThrow("Request cancelled");
    expect(calls).toHaveLength(2);
    expect(session.connected()).toBe(false);
    expect(session.usage().unknownCost).toBe(true);
  });

  it("accepts only explicit HTTPS or loopback service origins", () => {
    expect(trialBase("https://source.example")).toBe("https://source.example");
    expect(trialBase("http://127.0.0.1:8001")).toBe("http://127.0.0.1:8001");
    expect(trialBase("http://localhost:8001")).toBe("http://localhost:8001");
    for (const url of [
      "http://public.example",
      "http://[::1]:8001",
      "https://[::1]:8001",
      "https://key@source.example",
      "https://source.example/?key=secret",
      "https://source.example/path",
      "//source.example",
    ])
      expect(trialBase(url)).toBeNull();
  });
});
