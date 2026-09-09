import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import { EventDetails } from "./event-details";

function event() {
  const parsed = parseRunTrace(loadTraceFixture());
  if (!parsed.ok) throw new Error("fixture must be valid");
  const selected = parsed.trace.events.find(
    (item) => item.redacted_input !== null && item.operation_id !== null,
  );
  if (!selected) throw new Error("fixture must include redacted evidence");
  return selected;
}

describe("EventDetails", () => {
  it("shows only recorded redacted evidence and causal identifiers", () => {
    render(<EventDetails event={event()} />);

    const inspector = screen.getByRole("complementary", { name: "Recorded evidence" });
    expect(inspector).toHaveTextContent("Redacted input");
    expect(inspector).toHaveTextContent("Input hash");
    expect(inspector).toHaveTextContent("Operation");
    expect(inspector).toHaveTextContent("Structured rationale");
    expect(inspector).not.toHaveTextContent(/chain.of.thought|AI reasoning/i);
  });

  it("renders hostile-looking recorded text as inert evidence", () => {
    const hostile = {
      ...event(),
      rationale: { recorded: '<img src=x onerror="alert(1)">' },
    };

    render(<EventDetails event={hostile} />);

    expect(screen.getByText(/<img src=x/)).toBeVisible();
    expect(document.querySelector("img")).toBeNull();
  });
});
