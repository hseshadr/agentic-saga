import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { loadIndexFixture } from "../test/load-index-fixture";
import { parseScenarioIndex, ScenarioRepository } from "./repository";
import type { ScenarioIndex } from "./schema";

const TRACE_PATH = resolve(
  process.cwd(),
  "../../examples/ecommerce/flight-recorder/traces/happy-path.json",
);

function jsonResponse(body: string, status = 200): Response {
  return new Response(body, {
    headers: { "content-type": "application/json" },
    status,
  });
}

function validIndex(): Record<string, unknown> {
  return structuredClone(loadIndexFixture()) as Record<string, unknown>;
}

function firstRun(index: Record<string, unknown>): Record<string, unknown> {
  return (index.runs as Record<string, unknown>[])[0] ?? {};
}

function indexedRun(index: ScenarioIndex): ScenarioIndex["runs"][number] {
  const entry = index.runs[0];
  if (!entry) throw new Error("fixture must contain one run");
  return entry;
}

describe("parseScenarioIndex", () => {
  it("accepts a generic index that points to a real RunTrace", () => {
    const result = parseScenarioIndex(validIndex());

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.runs[0]?.id).toBe("happy-path");
  });

  it("rejects duplicate run IDs, extra fields, and oversized catalogs", () => {
    const duplicate = validIndex();
    duplicate.runs = [firstRun(duplicate), firstRun(duplicate)];
    expect(parseScenarioIndex(duplicate)).toEqual(
      expect.objectContaining({ message: expect.stringContaining("unique"), ok: false }),
    );

    expect(parseScenarioIndex({ ...validIndex(), scenarios: [] })).toEqual(
      expect.objectContaining({ ok: false }),
    );
    expect(
      parseScenarioIndex({ ...validIndex(), runs: Array(101).fill(firstRun(validIndex())) }),
    ).toEqual(expect.objectContaining({ ok: false }));
  });

  it("accepts an included default run and rejects an unknown default", () => {
    expect(parseScenarioIndex({ ...validIndex(), default_run_id: "business-failure" }).ok).toBe(
      true,
    );
    expect(parseScenarioIndex({ ...validIndex(), default_run_id: "missing" })).toEqual(
      expect.objectContaining({ ok: false }),
    );
  });

  it("accepts only the known optional presentation", () => {
    const ecommerce = validIndex();
    firstRun(ecommerce).presentation = "ecommerce";
    expect(parseScenarioIndex(ecommerce)).toEqual(expect.objectContaining({ ok: true }));

    const unknown = validIndex();
    firstRun(unknown).presentation = "banking";
    expect(parseScenarioIndex(unknown)).toEqual(expect.objectContaining({ ok: false }));
  });

  it("accepts explicitly unknown agent provenance", () => {
    const index = validIndex();
    firstRun(index).mode = "unknown";
    expect(parseScenarioIndex(index)).toEqual(expect.objectContaining({ ok: true }));
  });

  it.each([
    "/absolute.json",
    ".json",
    "../escape.json",
    "%2e%2e.json",
    "nested/trace.json",
    "nested\\trace.json",
    "https:trace.json",
    "trace.json?query",
    "trace.json#fragment",
  ])("rejects unsafe trace reference %s", (traceRef) => {
    const index = validIndex();
    firstRun(index).trace_ref = traceRef;
    expect(parseScenarioIndex(index)).toEqual(expect.objectContaining({ ok: false }));
  });
});

describe("ScenarioRepository", () => {
  it("invokes browser fetch with the Window receiver", async () => {
    const fetcher = vi.fn(function (this: typeof globalThis) {
      if (this !== globalThis) throw new TypeError("illegal invocation");
      return Promise.resolve(jsonResponse(JSON.stringify(validIndex())));
    }) as typeof fetch;
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    await expect(repository.loadIndex()).resolves.toEqual(expect.objectContaining({ ok: true }));
  });

  it("loads and verifies a real indexed trace", async () => {
    const trace = readFileSync(TRACE_PATH, "utf8");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(JSON.stringify(validIndex())))
      .mockResolvedValueOnce(jsonResponse(trace));
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    const index = await repository.loadIndex();
    expect(index.ok).toBe(true);
    if (!index.ok) return;
    const result = await repository.loadTrace(indexedRun(index.value));

    expect(result.ok).toBe(true);
    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
      "https://recorder.test/traces/index.json",
      "https://recorder.test/traces/happy-path.json",
    ]);
  });

  it("fails closed before parsing a trace whose digest was changed", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse("{}"));
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);
    const parsed = parseScenarioIndex(validIndex());
    if (!parsed.ok) throw new Error("fixture must be valid");

    await expect(repository.loadTrace(indexedRun(parsed.value))).resolves.toEqual({
      message: "RunTrace content does not match its index digest.",
      ok: false,
    });
  });

  it("rejects non-JSON, failed, and oversized responses without echoing content", async () => {
    const secret = "Bearer secret-value";
    const failed = vi.fn<typeof fetch>().mockResolvedValue(new Response(secret, { status: 500 }));
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), failed);
    const result = await repository.loadIndex();
    expect(result).toEqual({ message: "Trace index could not be loaded.", ok: false });
    expect(JSON.stringify(result)).not.toContain(secret);

    const oversized = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("{}", {
        headers: { "content-length": "600000", "content-type": "application/json" },
      }),
    );
    const bounded = new ScenarioRepository(new URL("https://recorder.test/traces/"), oversized);
    await expect(bounded.loadIndex()).resolves.toEqual(
      expect.objectContaining({ message: expect.stringContaining("safety bounds"), ok: false }),
    );
  });

  it("enforces the streamed byte limit when Content-Length is absent", async () => {
    const body = `{"value":"${"x".repeat(512 * 1024)}"}`;
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body));
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    await expect(repository.loadIndex()).resolves.toEqual({
      message: "Trace response exceeds recorder safety bounds.",
      ok: false,
    });
  });

  it.each([
    [
      new Response(null, { headers: { "content-type": "application/json" } }),
      "Trace response has no body.",
    ],
    [
      new Response("{}", { headers: { "content-type": "text/plain" } }),
      "Trace response must be JSON.",
    ],
    [jsonResponse("{"), "Trace response contains invalid JSON."],
    [
      jsonResponse('{"schema_version":"1.0","schema_version":"2.0"}'),
      "Trace response contains invalid JSON.",
    ],
  ])("fails closed for malformed responses", async (response, message) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response);
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    await expect(repository.loadIndex()).resolves.toEqual({ message, ok: false });
  });

  it("reports a failed trace fetch as a trace failure", async () => {
    const parsed = parseScenarioIndex(validIndex());
    if (!parsed.ok) throw new Error("fixture must be valid");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse("{}", 503));
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    await expect(repository.loadTrace(indexedRun(parsed.value))).resolves.toEqual({
      message: "RunTrace could not be loaded.",
      ok: false,
    });
  });
});
