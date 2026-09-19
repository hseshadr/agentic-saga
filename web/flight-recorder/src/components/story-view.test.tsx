import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { projectReplay } from "../replay/project-replay";
import { loadTraceFixture, type TraceFixtureName } from "../test/load-trace-fixture";
import { repeatProjectedEvents } from "../test/projected-events";
import { parseRunTrace } from "../trace/parse-run-trace";
import { StoryView } from "./story-view";

function projection(name: TraceFixtureName = "business-failure") {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectReplay(parsed.trace, 99);
}

describe("StoryView", () => {
  it("tells a deterministic story from recorded events in ledger order", () => {
    const projected = projection();
    render(<StoryView events={projected.flight.orderedEvents} onSelectEvent={vi.fn()} />);

    const story = screen.getByRole("table", { name: "Causal story" });
    const items = within(story).getAllByRole("row").slice(1);
    expect(items).toHaveLength(projected.events.length);
    expect(
      within(story)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Step", "Action", "Responsible", "Result", "Evidence"]);
    expect(items[0]).toHaveTextContent("01Saga started");
    expect(items[0]).toHaveTextContent("RecordedWorkflow started");
    expect(screen.getByText("Recovery started")).toBeVisible();
    expect(screen.getByText("Agent chose to reserve inventory")).toBeVisible();
    expect(screen.getByText("Inventory reserved")).toBeVisible();
    expect(screen.getByText("Payment charged")).toBeVisible();
    expect(screen.getByText("Delivery arranged")).toBeVisible();
    expect(screen.getByText("Order verification failed").closest("tr")).toHaveTextContent("Failed");
    expect(screen.getByText("Refund completed").closest("tr")).toHaveTextContent("Completed");
    expect(story).not.toHaveTextContent(/running|Then:/i);
    expect(story).not.toHaveTextContent(/chain.of.thought|AI reasoning/i);
    expect(story).not.toHaveTextContent(/kernel/i);
  });

  it("selects the exact recorded event for inspection", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    const projected = projection();
    render(<StoryView events={projected.flight.orderedEvents} onSelectEvent={select} />);

    const compensation = projected.events.find(
      ({ event_type }) => event_type === "compensation_started",
    );
    if (!compensation) throw new Error("fixture must contain compensation evidence");
    screen.getByRole("button", { name: `Inspect story event ${compensation.saga_seq}` }).focus();
    await user.keyboard("{Enter}");

    expect(select).toHaveBeenCalledWith(compensation.event_id);
  });

  it("bounds a large story while preserving the newest replay evidence", () => {
    const repeated = repeatProjectedEvents(500);
    render(<StoryView events={repeated} onSelectEvent={vi.fn()} />);

    expect(screen.getAllByRole("row")).toHaveLength(201);
    expect(screen.getByText("Showing the latest 200 of 500 visible events.")).toBeVisible();
    expect(screen.getByRole("cell", { name: "301" })).toBeVisible();
  });

  it("preserves the uncertain payment event after later reconciliation confirms payment", () => {
    const projected = projection("lost-response");
    render(<StoryView events={projected.flight.orderedEvents} onSelectEvent={vi.fn()} />);

    const uncertain = screen.getByText("Provider outcome not confirmed at this step").closest("tr");
    expect(uncertain).toHaveTextContent("Uncertain");
    expect(uncertain).not.toHaveTextContent("Completed");
    expect(
      screen.getByText("Payment charged; confirmed by reconciliation").closest("tr"),
    ).toHaveTextContent("Completed");
    expect(screen.getByText("Order verification passed").closest("tr")).toHaveTextContent(
      "Verified",
    );
  });

  it("distinguishes unresolved recovery and human review from completed undo actions", () => {
    render(
      <StoryView
        events={projection("compensation-failure").flight.orderedEvents}
        onSelectEvent={vi.fn()}
      />,
    );

    expect(
      screen.getByText("Provider could not reconcile the outcome").closest("tr"),
    ).toHaveTextContent("Uncertain");
    expect(
      screen.getByText("Human review requested before further changes").closest("tr"),
    ).toHaveTextContent("Needs review");
    expect(screen.queryByText("Refund completed")).not.toBeInTheDocument();
  });

  it("adds later events without rewriting successful forward results", () => {
    const events = projection().flight.orderedEvents;
    const { rerender } = render(<StoryView events={events.slice(0, 5)} onSelectEvent={vi.fn()} />);
    const payment = screen.getByText("Payment charged").closest("tr")?.textContent;
    expect(screen.queryByText("Refund completed")).not.toBeInTheDocument();

    rerender(<StoryView events={events} onSelectEvent={vi.fn()} />);

    expect(screen.getByText("Payment charged").closest("tr")?.textContent).toBe(payment);
    expect(screen.getByText("Refund completed")).toBeVisible();
  });

  it.each([
    [null, "Recorded", "Evidence recorded; no confirmed result in this event"],
    ["effect_confirmed", "Completed", "Provider confirmed the change"],
    ["no_effect_confirmed", "Failed", "Provider confirmed no change was made"],
    ["partial_effect_confirmed", "Uncertain", "Only part of the requested change was confirmed"],
  ])("uses recorded output for unknown tools (%s)", (kind, label, detail) => {
    const source = projection().flight.orderedEvents[2];
    if (!source) throw new Error("fixture must include an effect");
    const item = {
      ...source,
      event: {
        ...source.event,
        tool_name: "custom_action",
        redacted_output: kind ? { kind } : null,
      },
    };
    render(<StoryView events={[item]} onSelectEvent={vi.fn()} />);

    expect(screen.getByText("Custom action")).toBeVisible();
    expect(screen.getByText(detail).closest("tr")).toHaveTextContent(label ?? "");
  });
});
