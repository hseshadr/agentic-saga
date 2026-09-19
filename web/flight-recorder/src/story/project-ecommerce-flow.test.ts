import { describe, expect, it } from "vitest";
import { projectReplay } from "../replay/project-replay";
import { loadTraceFixture, type TraceFixtureName } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import { projectEcommerceFlow } from "./project-ecommerce-flow";

function flow(name: TraceFixtureName, cursor = 999) {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectEcommerceFlow(projectReplay(parsed.trace, cursor));
}

function cursorFor(name: TraceFixtureName, eventType: string, toolName?: string): number {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error("fixture must be valid");
  const cursor = parsed.trace.events.findIndex(
    ({ event_type, tool_name }) =>
      event_type === eventType && (!toolName || tool_name === toolName),
  );
  if (cursor < 0) throw new Error(`fixture must include ${eventType}`);
  return cursor;
}

describe("projectEcommerceFlow", () => {
  it("explains a verified happy path with no invented rollback", () => {
    const result = flow("happy-path");

    expect(result.headline).toBe("Order completed safely");
    expect(result.phase).toBe("succeeded");
    expect(result.forward.map(({ state }) => state)).toEqual([
      "complete",
      "complete",
      "complete",
      "complete",
    ]);
    expect(result.rollback.map(({ state }) => state)).toEqual(["waiting", "waiting", "waiting"]);
  });

  it("shows verified effects being undone in reverse order", () => {
    const result = flow("business-failure");

    expect(result.headline).toBe("Every completed change was safely undone");
    expect(result.phase).toBe("compensated");
    expect(result.forward.map(({ state }) => state)).toEqual([
      "reversed",
      "reversed",
      "reversed",
      "failed",
    ]);
    expect(result.rollback.map(({ id, state }) => [id, state])).toEqual([
      ["cancel-fulfillment", "complete"],
      ["refund-payment", "complete"],
      ["release-inventory", "complete"],
    ]);
  });

  it("makes a lost response visibly uncertain until reconciliation proves the charge", () => {
    const uncertain = flow(
      "lost-response",
      cursorFor("lost-response", "effect_outcome_recorded", "charge_payment"),
    );
    const reconciled = flow("lost-response", cursorFor("lost-response", "reconciliation_recorded"));

    expect(uncertain.forward.find(({ id }) => id === "charge-payment")?.state).toBe("checking");
    expect(uncertain.headline).toBe("Checking what the payment provider actually did");
    expect(reconciled.forward.find(({ id }) => id === "charge-payment")?.state).toBe("complete");
    expect(reconciled.headline).toBe("Provider evidence confirms the payment completed");
  });

  it("stops at the uncertain refund and asks a human instead of guessing", () => {
    const result = flow("compensation-failure");

    expect(result.headline).toBe("Paused safely for human review");
    expect(result.phase).toBe("human");
    expect(result.rollback.map(({ state }) => state)).toEqual(["complete", "attention", "waiting"]);
  });

  it("distinguishes provider checking from recorded human review for any compensation", () => {
    const parsed = parseRunTrace(loadTraceFixture("compensation-failure"));
    if (!parsed.ok) throw new Error("fixture must be valid");
    const trace = {
      ...parsed.trace,
      events: parsed.trace.events.map((event) => ({
        ...event,
        tool_name: event.tool_name === "refund_payment" ? "release_inventory" : event.tool_name,
      })),
    };
    const uncertainCursor = trace.events.findIndex(
      (event) =>
        event.tool_name === "release_inventory" &&
        event.event_type === "compensation_outcome_recorded",
    );
    const checking = projectEcommerceFlow(projectReplay(trace, uncertainCursor));
    const human = projectEcommerceFlow(projectReplay(trace, trace.events.length - 1));

    expect(checking.rollback.find(({ id }) => id === "release-inventory")?.state).toBe("checking");
    expect(human.rollback.find(({ id }) => id === "release-inventory")?.state).toBe("attention");
    expect(human.detail).toContain("recovery result");
  });

  it("shows only evidence visible at the current replay position", () => {
    const result = flow("business-failure", 0);

    expect(result.headline).toBe("The agent is reading the order goal");
    expect(result.forward.every(({ state }) => state === "waiting")).toBe(true);
    expect(result.rollback.every(({ state }) => state === "waiting")).toBe(true);
  });
});
