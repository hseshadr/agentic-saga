import { describe, expect, it } from "vitest";
import { projectReplay } from "../replay/project-replay";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import type { JsonObject, RunTrace, SagaStatus } from "../trace/schema";

function projection(name: "business-failure" | "compensation-failure", cursor = 99) {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectReplay(parsed.trace, cursor);
}

function businessTrace(): RunTrace {
  const parsed = parseRunTrace(loadTraceFixture("business-failure"));
  if (!parsed.ok) throw new Error("fixture must be valid");
  return parsed.trace;
}

function replaceInvariant(trace: RunTrace, rationale: JsonObject): RunTrace {
  const events = trace.events.map((event) =>
    event.event_type === "invariant_evaluated" ? { ...event, rationale } : event,
  );
  return { ...trace, events };
}

describe("recorded proof evaluation", () => {
  it("binds each expected rule to its exact visible source and terminal target", () => {
    const proof = projection("business-failure").proof;

    expect(proof.expectedRuleIds).toEqual([
      "inventory_released",
      "order_cancelled",
      "payment_refunded",
    ]);
    expect(proof.validRuleIds).toEqual(proof.expectedRuleIds);
    expect(proof.invalidRuleIds).toEqual([]);
    expect(proof.missingRuleIds).toEqual([]);
    expect(proof.sourceEvent?.saga_seq).toBe(36);
    expect(proof.terminalVerified).toBe(true);
  });

  it("does not reveal proof before its invariant event", () => {
    const proof = projection("business-failure", 34).proof;
    expect(proof.expectedRuleIds).toEqual([]);
    expect(proof.records).toEqual([]);
    expect(proof.terminalVerified).toBe(false);
  });

  it("keeps a recorded human stop separate from terminal proof", () => {
    const proof = projection("compensation-failure").proof;
    expect(proof.humanRequired).toBe(true);
    expect(proof.terminalVerified).toBe(false);
  });

  it("rejects proof bound to a different terminal state than the replay", () => {
    const trace = businessTrace();
    const target: SagaStatus = "succeeded_verified";
    const source = trace.events.findLast(({ event_type }) => event_type === "invariant_evaluated");
    if (!source) throw new Error("fixture must include invariant evidence");
    const changed = replaceInvariant(trace, { ...source.rationale, target_status: target });
    const proofs = changed.proofs.map((proof) => ({ ...proof, target_status: target }));

    expect(projectReplay({ ...changed, proofs }, 99).terminalVerified).toBe(false);
  });

  it("rejects an all-passed claim that contradicts its recorded rule results", () => {
    const trace = businessTrace();
    const source = trace.events.findLast(({ event_type }) => event_type === "invariant_evaluated");
    if (!source) throw new Error("fixture must include invariant evidence");
    const results = {
      inventory_released: true,
      order_cancelled: true,
      payment_refunded: false,
    };
    const changed = replaceInvariant(trace, { ...source.rationale, all_passed: true, results });

    expect(projectReplay(changed, 99).terminalVerified).toBe(false);
  });
});
