import { describe, expect, it } from "vitest";
import { loadIndexFixture } from "../test/load-index-fixture";
import { loadTraceFixture, type TraceFixtureName } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import type { RunTrace, TraceEvent } from "../trace/schema";
import { assessScenario, supportsScenario } from "./assess-scenario";
import { scenarioIndexSchema } from "./schema";

function trace(name: TraceFixtureName): RunTrace {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error("Expected a valid fixture");
  return parsed.trace;
}

function entry(name: TraceFixtureName) {
  const result = scenarioIndexSchema
    .parse(loadIndexFixture())
    .runs.find((item) => item.id === name);
  if (!result) throw new Error("Expected fixture catalog entry");
  return result;
}

function forwardEffect(trace: RunTrace, tool: string): string {
  const effect = trace.events.find(
    (event) => event.tool_name === tool && event.event_type === "effect_outcome_recorded",
  );
  if (!effect?.operation_id) throw new Error(`Expected a confirmed ${tool} effect`);
  return effect.operation_id;
}

function resolvedCompensation(): RunTrace {
  const paused = trace("compensation-failure");
  const recovered = trace("business-failure");
  const refund = paused.events.findLast((event) => event.tool_name === "refund_payment");
  const pause = paused.events.at(-1);
  if (!refund || !pause) throw new Error("Expected refund and pause evidence");
  const resolution: TraceEvent = {
    ...pause,
    event_id: "evt_humanresolution0123456789",
    event_type: "human_resolved",
    saga_seq: pause.saga_seq + 1,
    authority: "human",
    before_status: "human_required",
    after_status: "compensating",
    operation_id: refund.operation_id,
  };
  const compensated = ["schedule_fulfillment", "charge_payment", "reserve_inventory"].map((tool) =>
    forwardEffect(paused, tool),
  );
  const suffix = recovered.events.slice(-3).map((event, index) => ({
    ...event,
    trace_id: paused.run_id,
    saga_seq: resolution.saga_seq + index + 1,
    compensates_operation_id:
      event.tool_name === "release_inventory"
        ? forwardEffect(paused, "reserve_inventory")
        : event.compensates_operation_id,
    rationale:
      event.event_type === "compensation_completed"
        ? { ...event.rationale, compensated_operation_ids: compensated }
        : event.rationale,
  }));
  return {
    ...paused,
    outcome: "compensated_verified",
    finished_at: recovered.finished_at,
    events: [...paused.events, resolution, ...suffix],
  };
}

describe("scenario assessment", () => {
  it.each<TraceFixtureName>([
    "happy-path",
    "business-failure",
    "lost-response",
    "compensation-failure",
  ])("passes the recorded safety expectation for %s", (name) => {
    expect(assessScenario(entry(name), trace(name)).state).toBe("passed");
  });

  it("explains a passed safe-stop scenario without claiming the order recovered", () => {
    const result = assessScenario(entry("compensation-failure"), trace("compensation-failure"));
    expect(result.label).toBe("Safety check passed");
    expect(result.detail).toBe("Safe stop demonstrated — human review remains required.");
  });

  it("requires terminal proof, not just a successful outcome name", () => {
    for (const name of ["happy-path", "lost-response"] as const) {
      expect(assessScenario(entry(name), { ...trace(name), proofs: [] }).state).toBe("failed");
    }
  });

  it("requires a confirmed outcome for every undo step, not a compensated outcome name", () => {
    const recovered = trace("business-failure");
    const events = recovered.events.filter(
      (event) => event.event_type !== "compensation_outcome_recorded",
    );
    expect(assessScenario(entry("business-failure"), { ...recovered, events })).toMatchObject({
      state: "failed",
      detail: "The recovery lacks a confirmed compensation outcome for every undone change.",
    });
  });

  it("rejects outcomes that do not match the scenario expectation", () => {
    expect(assessScenario(entry("happy-path"), trace("business-failure")).state).toBe("failed");
    expect(assessScenario(entry("business-failure"), trace("happy-path")).state).toBe("failed");
    expect(assessScenario(entry("happy-path"), trace("compensation-failure")).state).toBe("failed");
  });

  it("requires confirmed reconciliation of the same lost payment operation", () => {
    const lost = trace("lost-response");
    const unrelated = lost.events.map((event) =>
      event.event_type === "reconciliation_recorded" ? { ...event, operation_id: null } : event,
    );
    expect(assessScenario(entry("lost-response"), trace("happy-path")).state).toBe("failed");
    expect(assessScenario(entry("lost-response"), { ...lost, events: unrelated }).state).toBe(
      "failed",
    );
  });

  it("requires actual uncertain refund evidence for a passed safe stop", () => {
    const stopped = trace("compensation-failure");
    const events = stopped.events.filter((event) => event.tool_name !== "refund_payment");
    expect(assessScenario(entry("compensation-failure"), { ...stopped, events }).state).toBe(
      "failed",
    );
  });

  it("accepts human resolution followed by verified completed recovery", () => {
    expect(assessScenario(entry("compensation-failure"), resolvedCompensation())).toMatchObject({
      state: "passed",
      label: "Passed",
    });
  });

  it("rejects recovery that continued before the uncertain refund was resolved", () => {
    const resolved = resolvedCompensation();
    const events = resolved.events.map((event) =>
      event.tool_name === "release_inventory" ? { ...event, saga_seq: 14 } : event,
    );
    expect(assessScenario(entry("compensation-failure"), { ...resolved, events }).state).toBe(
      "failed",
    );
  });

  it("does not green-check a safe-stop claim with unrelated payment evidence", () => {
    const stopped = trace("compensation-failure");
    const events = stopped.events.map((event) =>
      event.tool_name === "refund_payment" ? { ...event, compensates_operation_id: null } : event,
    );
    expect(assessScenario(entry("compensation-failure"), { ...stopped, events }).state).toBe(
      "failed",
    );
  });

  it("rejects missing or unrelated human resolution even with completed recovery", () => {
    const resolved = resolvedCompensation();
    const events = resolved.events.map((event) =>
      event.event_type === "human_resolved" ? { ...event, operation_id: null } : event,
    );
    expect(assessScenario(entry("compensation-failure"), { ...resolved, events }).state).toBe(
      "failed",
    );
    expect(assessScenario(entry("compensation-failure"), trace("business-failure")).state).toBe(
      "failed",
    );
    const unrecorded = resolved.events.filter(
      (event) => event.event_type !== "compensation_completed",
    );
    expect(
      assessScenario(entry("compensation-failure"), { ...resolved, events: unrecorded }).state,
    ).toBe("failed");
  });

  it("keeps recorded work in progress pending", () => {
    const running = { ...trace("happy-path"), outcome: "running" as const, finished_at: null };
    expect(assessScenario(entry("happy-path"), running).state).toBe("pending");
  });

  it("does not invent expected test outcomes for unknown or generic recordings", () => {
    const generic = { ...entry("happy-path"), presentation: undefined };
    const unknown = { ...entry("happy-path"), id: "another-scenario" };
    expect(supportsScenario(generic)).toBe(false);
    expect(supportsScenario(unknown)).toBe(false);
    expect(assessScenario(generic, trace("happy-path"))).toMatchObject({
      state: "unavailable",
      label: "Verified",
    });
    expect(assessScenario(unknown, trace("compensation-failure"))).toMatchObject({
      state: "unavailable",
      label: "No assessment",
    });
  });
});
