import { describe, expect, it } from "vitest";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { causalEventIds, projectFlight } from "./flight-projection";
import { parseRunTrace } from "./parse-run-trace";

function projection() {
  const parsed = parseRunTrace(loadTraceFixture());
  if (!parsed.ok) throw new Error(parsed.message);
  return projectFlight(parsed.trace);
}

describe("projectFlight", () => {
  it("projects every ledger event once into four ordered lanes", () => {
    const result = projection();
    const projected = result.lanes.flatMap((lane) => lane.events);

    expect(result.lanes.map((lane) => lane.id)).toEqual(["agent", "guard", "effect", "proof"]);
    expect(projected).toHaveLength(37);
    expect(new Set(projected.map((item) => item.event.event_id))).toHaveLength(37);
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
      ?.events.find((item) => item.event.event_type === "compensation_intent_recorded");

    expect(repair?.signal).toBe("compensation");
    expect(repair?.event.compensates_operation_id).toMatch(/^op_/);
  });

  it("isolates a selected compensation and its recorded forward causal chain", () => {
    const result = projection();
    const selected = result.orderedEvents.find(
      (item) => item.event.event_type === "compensation_intent_recorded",
    );
    if (!selected) throw new Error("fixture must include compensation evidence");

    const ids = causalEventIds(result, selected.event.event_id);
    const events = result.orderedEvents.filter((item) => ids.has(item.event.event_id));

    expect(events.some((item) => item.event.direction === "forward")).toBe(true);
    expect(events.some((item) => item.event.direction === "compensation")).toBe(true);
    expect(events.every((item) => item.event.operation_id !== null)).toBe(true);
  });

  it("binds proof rows to their recorded invariant event without inventing proposals", () => {
    const result = projection();

    expect(result.proofs).toHaveLength(3);
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
  });

  it("projects real human-required evidence as a distinct fault signal", () => {
    const parsed = parseRunTrace(loadTraceFixture("compensation-failure"));
    if (!parsed.ok) throw new Error(parsed.message);

    const human = projectFlight(parsed.trace).orderedEvents.find(
      (item) => item.event.authority === "human",
    );

    expect(human?.lane).toBe("proof");
    expect(human?.signal).toBe("fault");
    expect(parsed.trace.finished_at).toBeNull();
  });
});
