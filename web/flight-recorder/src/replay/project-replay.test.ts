import { describe, expect, it } from "vitest";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import { projectReplay } from "./project-replay";

function trace(name: "business-failure" | "compensation-failure" = "business-failure") {
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
    const projection = projectReplay(trace(), 999);

    expect(projection.cursor).toBe(trace().events.length - 1);
    expect(projection.proofs).toHaveLength(1);
    expect(projection.proofs[0]?.rule_id).toBe("obligations_reversed");
    expect(projection.terminalVerified).toBe(true);
    expect(projection.currentStatus).toBe("compensated_verified");
  });

  it("fails closed when recorded proof omits a declared invariant", () => {
    const source = trace();
    const incomplete = { ...source, proofs: source.proofs.slice(0, -1) };

    const projection = projectReplay(incomplete, 999);

    expect(projection.proof.expectedRuleIds).toEqual(["obligations_reversed"]);
    expect(projection.proof.missingRuleIds).toEqual(["obligations_reversed"]);
    expect(projection.terminalVerified).toBe(false);
  });

  it("uses only the latest invariant evaluation's exact proof group", () => {
    const source = trace();
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
    const source = trace();
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
