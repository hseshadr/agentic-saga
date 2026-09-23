import { describe, expect, it } from "vitest";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import type { RunTrace, TraceEvent } from "../trace/schema";
import { evaluateRecovery } from "./evaluate-recovery";

function trace(): RunTrace {
  const parsed = parseRunTrace(loadTraceFixture("business-failure"));
  if (!parsed.ok) throw new Error("fixture must be valid");
  return parsed.trace;
}

function completion(events: readonly TraceEvent[]): TraceEvent {
  const record = events.find(({ event_type }) => event_type === "compensation_completed");
  if (!record) throw new Error("fixture must record completed compensation");
  return record;
}

describe("recorded recovery evaluation", () => {
  it("confirms every compensated operation from its own recorded compensation receipt", () => {
    const { events } = trace();
    const recovery = evaluateRecovery(events, "compensated_verified", true);

    expect(recovery.record?.event_id).toBe(completion(events).event_id);
    expect(recovery.compensatedOperationIds).toHaveLength(3);
    expect(recovery.confirmedOperationIds).toEqual(recovery.compensatedOperationIds);
    expect(recovery.completed).toBe(true);
  });

  it("claims no invariant proof and no terminal result before the record is visible", () => {
    const { events } = trace();
    const before = events.slice(0, completion(events).saga_seq - 1);
    const recovery = evaluateRecovery(before, "compensating", false);

    expect(recovery.record).toBeUndefined();
    expect(recovery.completed).toBe(false);
  });

  it("fails closed when a listed compensation has no confirmed outcome", () => {
    const events = trace().events.map((event) =>
      event.tool_name === "refund_payment"
        ? { ...event, redacted_output: { kind: "outcome_unknown" } }
        : event,
    );
    const recovery = evaluateRecovery(events, "compensated_verified", true);

    expect(recovery.confirmedOperationIds).toHaveLength(2);
    expect(recovery.completed).toBe(false);
  });

  it("accepts an unknown compensation only after a matching human resolution", () => {
    const source = trace().events;
    const refund = source.find(({ tool_name }) => tool_name === "refund_payment");
    if (!refund) throw new Error("fixture must include a refund");
    const unknown = source.map((event) =>
      event === refund ? { ...event, redacted_output: { kind: "outcome_unknown" } } : event,
    );
    const resolution: TraceEvent = {
      ...refund,
      event_type: "human_resolved",
      saga_seq: source.length + 1,
      authority: "human",
      direction: null,
      compensates_operation_id: null,
    };
    const resolved = [...unknown, resolution];

    expect(evaluateRecovery(resolved, "compensated_verified", true).completed).toBe(true);
  });

  it("rejects a record bound to a different target or an empty compensation list", () => {
    const events = trace().events;
    const record = completion(events);
    const retarget = (rationale: TraceEvent["rationale"]) =>
      events.map((event) => (event === record ? { ...event, rationale } : event));

    const wrongTarget = retarget({ ...record.rationale, target_status: "human_required" });
    const empty = retarget({ ...record.rationale, compensated_operation_ids: [] });

    expect(evaluateRecovery(wrongTarget, "compensated_verified", true).completed).toBe(false);
    expect(evaluateRecovery(empty, "compensated_verified", true).completed).toBe(false);
    expect(evaluateRecovery(events, "compensated_verified", false).completed).toBe(false);
  });
});
