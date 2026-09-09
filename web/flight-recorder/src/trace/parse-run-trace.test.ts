import { describe, expect, it } from "vitest";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { parseTraceJson } from "./lossless-json";
import { parseRunTrace } from "./parse-run-trace";

function validTrace(): Record<string, unknown> {
  return structuredClone(loadTraceFixture()) as Record<string, unknown>;
}

function events(trace: Record<string, unknown>): Record<string, unknown>[] {
  return trace.events as Record<string, unknown>[];
}

function traceWithInput(source: string, digest: string): unknown {
  const trace = validTrace();
  const bound = events(trace).find((event) => event.redacted_input !== null);
  if (!bound) throw new Error("fixture must include hashed input");
  bound.redacted_input = "__JSON__";
  bound.input_hash = digest;
  return parseTraceJson(JSON.stringify(trace).replace('"__JSON__"', source));
}

describe("parseRunTrace", () => {
  it("accepts a real kernel-exported RunTrace 1.0", () => {
    const result = parseRunTrace(validTrace());

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.trace.events).toHaveLength(37);
  });

  it("rejects unsupported versions and extra aliases without throwing", () => {
    const versioned = { ...validTrace(), schema_version: "2.0" };
    expect(parseRunTrace(versioned)).toEqual({
      message: "This recorder supports RunTrace 1.0 only.",
      ok: false,
    });

    expect(parseRunTrace({ ...validTrace(), runId: "alias" })).toEqual(
      expect.objectContaining({ ok: false }),
    );
  });

  it("rejects a non-contiguous causal sequence", () => {
    const trace = validTrace();
    const second = events(trace)[1];
    if (second) second.saga_seq = 1;

    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace events must be contiguous and ordered.",
      ok: false,
    });
  });

  it("rejects non-UTC and descending event times", () => {
    const trace = validTrace();
    const second = events(trace)[1];
    if (second) second.recorded_at = "2025-12-31T23:59:59Z";

    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace event times must be monotonic UTC timestamps.",
      ok: false,
    });

    expect(parseRunTrace({ ...validTrace(), started_at: "2026-01-01T01:00:00+01:00" })).toEqual(
      expect.objectContaining({ ok: false }),
    );
  });

  it("orders UTC evidence without losing sub-millisecond precision", () => {
    const trace = validTrace();
    const second = events(trace)[1];
    const third = events(trace)[2];
    if (second) second.recorded_at = "2026-01-01T00:00:00.000002Z";
    if (third) third.recorded_at = "2026-01-01T00:00:00.000001Z";

    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace event times must be monotonic UTC timestamps.",
      ok: false,
    });
  });

  it("accepts Python-valid UTC timestamps with an explicit zero offset", () => {
    const trace = validTrace();
    trace.started_at = "2026-01-01T00:00:00+00:00";
    trace.finished_at = "2026-01-01T00:00:00+00:00";
    for (const event of events(trace)) event.recorded_at = "2026-01-01T00:00:00+00:00";

    expect(parseRunTrace(trace)).toEqual(expect.objectContaining({ ok: true }));
  });

  it("accepts Python's maximum UTC calendar year", () => {
    const trace = validTrace();
    trace.started_at = "9999-12-31T23:59:59Z";
    trace.finished_at = "9999-12-31T23:59:59Z";
    for (const event of events(trace)) event.recorded_at = "9999-12-31T23:59:59Z";

    expect(parseRunTrace(trace)).toEqual(expect.objectContaining({ ok: true }));
  });

  it.each([
    "2026-02-30T00:00:00Z",
    "2026-02-30T00:00:00+00:00",
    "2026-13-01T00:00:00Z",
    "2026-01-01T24:00:00Z",
    "2026-01-01T00:00:60Z",
    "0000-01-01T00:00:00Z",
    "0000-01-01T00:00:00+00:00",
  ])("rejects a normalized but impossible UTC timestamp %s", (timestamp) => {
    const trace = validTrace();
    const second = events(trace)[1];
    if (second) second.recorded_at = timestamp;

    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace contains an invalid UTC timestamp.",
      ok: false,
    });
  });

  it("rejects a broken status chain or terminal header", () => {
    const trace = validTrace();
    const second = events(trace)[1];
    if (second) second.before_status = "running";
    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace statuses must form one causal chain.",
      ok: false,
    });

    expect(parseRunTrace({ ...validTrace(), outcome: "succeeded_verified" })).toEqual({
      message: "RunTrace outcome does not match its final event.",
      ok: false,
    });
  });

  it("rejects proof records not bound to invariant evidence", () => {
    const trace = validTrace();
    const proof = (trace.proofs as Record<string, unknown>[])[0];
    if (proof) proof.source_event_seq = 1;

    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace proof source is absent or not invariant evidence.",
      ok: false,
    });
  });

  it("rejects redacted evidence whose canonical hash was changed", () => {
    const trace = validTrace();
    const bound = events(trace).find((event) => event.redacted_input !== null);
    if (bound) bound.redacted_input = { changed: true };

    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace evidence hash does not match its redacted value.",
      ok: false,
    });
  });

  it("matches the kernel's Unicode code-point ordering for canonical hashes", () => {
    const trace = validTrace();
    const bound = events(trace).find((event) => event.redacted_input !== null);
    if (!bound) throw new Error("fixture must include hashed input");
    bound.redacted_input = { "\ue000": "bmp", "😀": "astral" };
    bound.input_hash = "4874d36535a1b5dfdad7f2b3af83f87debd2f0795ae7d2d09447e17223c42dd0";

    expect(parseRunTrace(trace)).toEqual(expect.objectContaining({ ok: true }));
  });

  it("matches Python key ordering for integer-like evidence keys", () => {
    const source = '{"10":"ten","2":"two"}';
    const digest = "b71e124675fc80e7314688bffdb68e83515851fb51474125db9a0c4c8aca3808";

    expect(parseRunTrace(traceWithInput(source, digest))).toEqual(
      expect.objectContaining({ ok: true }),
    );
  });

  it.each([
    ["1.0", "3a7d647740ec6f86b72e0bf3948ab456551e07e9605e3a2785de1c66842ebb48"],
    ["1e+20", "ff6b79500a63501d96ff451fd1ab5191f8e1284f0f5265b2330b34badc35bcae"],
    ["1e-7", "aece37dfda4992947222ea73b79996bba4aa181345a1fbb7f8c2b56b3fbd6a44"],
    ["-0.0", "c848a4efa987f46ba3bfd46242333afcb1c68c3240e0f35ae9d269b1c980648b"],
    ["9007199254740993", "16943e87887a1f1c481ac6bb576c4fa30b70320d4684fd5435adef62d2c5364e"],
    ["0.00001", "ae608be085de5ef7c25d252b967f74344a8a3ab7743e361cfea45569321eb6b6"],
    ["1000000000000000.0", "fdf8fc7ac54b0c8205b61e7d06e65e6833ef93b9631da9e8d92ad12d59efbb29"],
    ["1e+16", "f6b1d8095563fb7f57c554825c9d5402bfc8f69255edfaed38587394a2252515"],
    ["0.0001", "4e8c736958cd287696c5db2674af41fd38337bb6aa1a732bbfe5d3ff3063a809"],
    ["1.234567890123456e+20", "7ca9fd83c2c5dd8f126d52e9137ea37692ed7380e709a19c73d2abe39a21a185"],
  ])("matches Python canonical hashes for numeric lexeme %s", (source, digest) => {
    expect(parseRunTrace(traceWithInput(`{"value":${source}}`, digest))).toEqual(
      expect.objectContaining({ ok: true }),
    );
  });

  it("matches Python canonical hashing for nested mixed numeric evidence", () => {
    const source = '{"nested":[{"value":1.0},{"large":9007199254740993}],"negative":-0.0}';
    const digest = "b48ad3b3e3d33631e02c140df4e969dd529047c8aa3b246492be5df65d854a07";

    expect(parseRunTrace(traceWithInput(source, digest))).toEqual(
      expect.objectContaining({ ok: true }),
    );
  });

  it.each([
    ["1", "0e0a019d1071c610aba8ad2691e0abd5ce2bffb12da1dab39504a48ccb460a6c"],
    ["1.0", "6f9ed6cd4362f16459de6f258a1e3e7cb5f8935497ff07098d6a2c24c5513c5f"],
    ["9007199254740993", "f60ce2479d1efe90dfa8e8cc50ab5c8849ed07284beb706e1af43d48ee4f9141"],
  ])("applies the same depth bound to every numeric form (%s)", (number, digest) => {
    const source = `${'{"x":'.repeat(12)}{"v":${number}}${"}".repeat(12)}`;

    expect(parseRunTrace(traceWithInput(source, digest))).toEqual(
      expect.objectContaining({ ok: true }),
    );
  });

  it("accepts a Python-valid fence token beyond JavaScript's safe integer range", () => {
    const source = JSON.stringify(validTrace()).replace(
      '"fence_token":1',
      '"fence_token":9007199254740993',
    );

    expect(parseRunTrace(parseTraceJson(source))).toEqual(expect.objectContaining({ ok: true }));
  });

  it.each(["semantic_generation", "attempt"])(
    "accepts a Python-valid unbounded %s integer losslessly",
    (field) => {
      const source = JSON.stringify(validTrace()).replace(
        new RegExp(`"${field}":\\d+`),
        `"${field}":9007199254740993`,
      );

      expect(parseRunTrace(parseTraceJson(source))).toEqual(expect.objectContaining({ ok: true }));
    },
  );

  it("hashes an ordinary object that resembles a lossless number as an object", () => {
    const source = '{"value":{"isLosslessNumber":true,"value":"1"}}';
    const digest = "b33a57eaa4bedf040a3a182f466061ec249c521aa8e97659a9b7354d9c57ae7c";

    expect(parseRunTrace(traceWithInput(source, digest))).toEqual(
      expect.objectContaining({ ok: true }),
    );
  });

  it("never silently drops prototype-like keys at the root or in evidence", () => {
    const root = JSON.stringify(validTrace()).replace(
      '{"schema_version"',
      '{"__proto__":null,"schema_version"',
    );
    expect(() => parseTraceJson(root)).toThrow();

    const nested = '{"nested":{"__proto__":null}}';
    const digestAsIfDropped = "2e5bd07caafc11220cb02f8e1288951f47fff8fbbdb2dad0c830aed9f767cf2e";
    expect(() => traceWithInput(nested, digestAsIfDropped)).toThrow();
  });

  it("rejects excessive depth, nodes, and strings before schema validation", () => {
    let nested: Record<string, unknown> = {};
    for (let index = 0; index < 18; index += 1) nested = { nested };
    expect(parseRunTrace(nested)).toEqual({
      message: "RunTrace exceeds recorder safety bounds.",
      ok: false,
    });

    expect(parseRunTrace({ value: "x".repeat(20_001) })).toEqual(
      expect.objectContaining({ ok: false }),
    );
  });

  it("fails closed for cyclic or hostile inputs without echoing them", () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(parseRunTrace(cyclic)).toEqual({ message: "RunTrace contains a cycle.", ok: false });

    const hostile = new Proxy(
      {},
      {
        ownKeys: () => {
          throw new Error("secret-provider-text");
        },
      },
    );
    const result = parseRunTrace(hostile);
    expect(result).toEqual({ message: "RunTrace could not be validated safely.", ok: false });
    if (result.ok) throw new Error("hostile input must fail closed");
    expect(result.message).not.toContain("secret-provider-text");
  });

  it("binds the trace header and definition to ledger evidence", () => {
    expect(parseRunTrace({ ...validTrace(), run_id: "trace_aaaaaaaaaaaaaaaa" })).toEqual({
      message: "RunTrace header does not match its first event.",
      ok: false,
    });
    expect(parseRunTrace({ ...validTrace(), definition_version: "other-v1" })).toEqual({
      message: "RunTrace definition changed during the run.",
      ok: false,
    });
  });

  it("rejects finish metadata or an initial state outside the causal chain", () => {
    expect(parseRunTrace({ ...validTrace(), finished_at: null })).toEqual({
      message: "RunTrace finish time does not match terminal evidence.",
      ok: false,
    });
    const trace = validTrace();
    const first = events(trace)[0];
    if (first) first.before_status = "created";
    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace statuses must form one causal chain.",
      ok: false,
    });
  });

  it("rejects redacted evidence without its matching hash", () => {
    const trace = validTrace();
    const bound = events(trace).find((event) => event.redacted_input !== null);
    if (bound) bound.input_hash = null;

    expect(parseRunTrace(trace)).toEqual({
      message: "RunTrace evidence hash does not match its redacted value.",
      ok: false,
    });
  });
});
