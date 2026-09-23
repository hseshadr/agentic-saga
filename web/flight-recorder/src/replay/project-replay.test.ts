import { describe, expect, it } from "vitest";
import { loadTraceFixture, type TraceFixtureName } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import { projectReplay } from "./project-replay";

function trace(name: TraceFixtureName = "business-failure") {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error("fixture must be valid");
  return parsed.trace;
}

describe("projectReplay", () => {
  it("never exposes future terminal or proof evidence before its ledger cursor", () => {
    const source = trace();
    const compensationIndex = source.events.findIndex(
      ({ event_type }) => event_type === "compensation_started",
    );
    const projection = projectReplay(source, compensationIndex);

    expect(projection.currentEvent.event_type).toBe("compensation_started");
    expect(projection.proofs.map(({ rule_id }) => rule_id)).toEqual(["verify_order"]);
    expect(projection.terminalVerified).toBe(false);
    expect(projection.currentStatus).toBe("compensating");
  });

  it("clamps to the recorded end and verifies only recorded terminal proof", () => {
    const projection = projectReplay(trace("happy-path"), 999);

    expect(projection.cursor).toBe(trace("happy-path").events.length - 1);
    expect(projection.proofs).toHaveLength(1);
    expect(projection.proofs[0]?.rule_id).toBe("verify_order");
    expect(projection.terminalVerified).toBe(true);
    expect(projection.currentStatus).toBe("succeeded_verified");
  });

  it("verifies compensated recovery from its receipts, not from an invariant proof", () => {
    const projection = projectReplay(trace(), 999);

    expect(projection.proofs.map(({ result, rule_id }) => [rule_id, result])).toEqual([
      ["verify_order", "invalid"],
    ]);
    expect(projection.recovery.record?.event_type).toBe("compensation_completed");
    expect(projection.recovery.confirmedOperationIds).toHaveLength(3);
    expect(projection.terminalVerified).toBe(true);
    expect(projection.currentStatus).toBe("compensated_verified");
  });

  it("does not verify a compensated terminal whose undo steps lack recorded outcomes", () => {
    const source = trace();
    const events = source.events.filter(
      ({ event_type }) => event_type !== "compensation_outcome_recorded",
    );
    const resequenced = events.map((event, index) => ({ ...event, saga_seq: index + 1 }));

    const projection = projectReplay({ ...source, events: resequenced }, 999);

    expect(projection.recovery.confirmedOperationIds).toEqual([]);
    expect(projection.terminalVerified).toBe(false);
  });

  it("fails closed when recorded proof omits a declared invariant", () => {
    const source = trace("happy-path");
    const incomplete = { ...source, proofs: source.proofs.slice(0, -1) };

    const projection = projectReplay(incomplete, 999);

    expect(projection.proof.expectedRuleIds).toEqual(["verify_order"]);
    expect(projection.proof.missingRuleIds).toEqual(["verify_order"]);
    expect(projection.terminalVerified).toBe(false);
  });

  it("uses only the latest invariant evaluation's exact proof group", () => {
    const source = trace("happy-path");
    const latest = source.events.findLast(({ event_type }) => event_type === "invariant_evaluated");
    if (!latest) throw new Error("fixture must contain proof evidence");
    const projection = projectReplay(source, 999);

    expect(projection.proofs).toHaveLength(1);
    expect(projection.proofs.every((proof) => proof.source_event_id === latest.event_id)).toBe(
      true,
    );
    expect(projection.terminalVerified).toBe(true);
  });

  it("fails closed and presents one row per rule when the latest group has a duplicate", () => {
    const source = trace("happy-path");
    const duplicate = source.proofs.at(-1);
    if (!duplicate) throw new Error("fixture must contain proof evidence");

    const projection = projectReplay({ ...source, proofs: [...source.proofs, duplicate] }, 999);

    expect(projection.terminalVerified).toBe(false);
    expect(new Set(projection.proofs.map((proof) => proof.rule_id)).size).toBe(
      projection.proofs.length,
    );
  });

  it("keeps HUMAN_REQUIRED quiescent and distinct from a terminal outcome", () => {
    const projection = projectReplay(trace("compensation-failure"), 999);

    expect(projection.humanRequired).toBe(true);
    expect(projection.terminalVerified).toBe(false);
    expect(projection.currentStatus).toBe("human_required");
  });

  it("fails closed if an unchecked caller bypasses the non-empty trace boundary", () => {
    const empty = { ...trace(), events: [] };

    expect(() => projectReplay(empty, 0)).toThrow(
      "validated RunTrace must contain ledger evidence",
    );
  });
});
