import { describe, expect, it } from "vitest";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { causalEventIds, projectFlight } from "./flight-projection";
import { parseRunTrace } from "./parse-run-trace";

function projection(name: "business-failure" | "compensation-failure" = "business-failure") {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error(parsed.message);
  return projectFlight(parsed.trace);
}

describe("projectFlight", () => {
  it("projects every ledger event once into four ordered lanes", () => {
    const result = projection();
    const projected = result.lanes.flatMap((lane) => lane.events);

    expect(result.lanes.map((lane) => lane.id)).toEqual(["agent", "guard", "effect", "proof"]);
    expect(result.lanes.find(({ id }) => id === "guard")?.label).toBe("Workflow guard");
    expect(projected).toHaveLength(result.orderedEvents.length);
    expect(new Set(projected.map((item) => item.event.event_id))).toHaveLength(
      result.orderedEvents.length,
    );
    for (const lane of result.lanes) {
      expect(lane.events.map((item) => item.event.saga_seq)).toEqual(
        [...lane.events].map((item) => item.event.saga_seq).sort((left, right) => left - right),
      );
    }
  });

  it("keeps recorded compensation evidence in the effect and repair lane", () => {
    const result = projection();
    const repair = result.lanes
      .find((lane) => lane.id === "effect")
      ?.events.find((item) => item.event.event_type === "compensation_outcome_recorded");

    expect(repair?.signal).toBe("compensation");
    expect(repair?.event.compensates_operation_id).toMatch(/^op_/);
  });

  it("isolates a selected compensation and its recorded forward causal chain", () => {
    const result = projection();
    const selected = result.orderedEvents.find(
      (item) => item.event.event_type === "compensation_outcome_recorded",
    );
    if (!selected) throw new Error("fixture must include compensation evidence");

    const ids = causalEventIds(result, selected.event.event_id);
    const events = result.orderedEvents.filter((item) => ids.has(item.event.event_id));

    const forward = events.find((item) => item.event.direction === "forward");
    const compensation = events.find((item) => item.event.direction === "compensation");
    expect(forward?.event.operation_id).toBe(compensation?.event.compensates_operation_id);
    expect(compensation?.event.event_type).toBe("compensation_outcome_recorded");
  });

  it("puts recorded agent decisions in the Agent lane with bounded plain-language labels", () => {
    const result = projection();
    const decisions = result.orderedEvents.filter(
      ({ event }) => event.event_type === "agent_decision_recorded",
    );

    expect(decisions.length).toBeGreaterThan(0);
    expect(decisions.every(({ lane }) => lane === "agent")).toBe(true);
    expect(decisions.map(({ label }) => label)).toContain("Agent chose to reserve inventory");
    expect(decisions.map(({ label }) => label).join(" ")).not.toMatch(
      /chain.of.thought|reasoning/i,
    );
  });

  it("binds proof rows to their recorded invariant event without inventing proposals", () => {
    const result = projection();

    expect(result.proofs.map(({ rule_id }) => rule_id)).toEqual(["verify_order"]);
    expect(new Set(result.proofs.map((proof) => proof.source_event_id))).toEqual(
      new Set(
        result.orderedEvents
          .filter((item) => item.event.event_type === "invariant_evaluated")
          .map((item) => item.event.event_id),
      ),
    );
    expect(result.orderedEvents.some((item) => item.event.event_type === "proposal_accepted")).toBe(
      false,
    );
    const completed = result.orderedEvents.find(
      (item) => item.event.event_type === "compensation_completed",
    );
    expect(completed?.lane).toBe("guard");
    expect(completed?.label).toBe("Compensation completed");
  });

  it("projects real human-required evidence as a distinct fault signal", () => {
    const result = projection("compensation-failure");
    const human = result.orderedEvents.find((item) => item.event.event_type === "human_required");

    expect(human?.event.authority).toBe("workflow");
    expect(human?.event.after_status).toBe("human_required");
    expect(human?.lane).toBe("proof");
    expect(human?.signal).toBe("fault");
  });
});
