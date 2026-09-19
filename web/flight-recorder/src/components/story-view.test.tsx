import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { projectReplay } from "../replay/project-replay";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { repeatProjectedEvents } from "../test/projected-events";
import { parseRunTrace } from "../trace/parse-run-trace";
import { StoryView } from "./story-view";

function projection() {
  const parsed = parseRunTrace(loadTraceFixture());
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectReplay(parsed.trace, 99);
}

describe("StoryView", () => {
  it("tells a deterministic story from recorded events in ledger order", () => {
    const projected = projection();
    render(<StoryView events={projected.flight.orderedEvents} onSelectEvent={vi.fn()} />);

    const story = screen.getByRole("list", { name: "Causal story" });
    const items = within(story).getAllByRole("listitem");
    expect(items).toHaveLength(projected.events.length);
    expect(items[0]).toHaveTextContent("01 Saga started");
    expect(screen.getByText("The workflow started safe compensation.")).toBeVisible();
    expect(screen.getByText("Agent chose to reserve inventory.")).toBeVisible();
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
    await user.click(
      screen.getByRole("button", { name: `Inspect story event ${compensation.saga_seq}` }),
    );

    expect(select).toHaveBeenCalledWith(compensation.event_id);
  });

  it("bounds a large story while preserving the newest replay evidence", () => {
    const repeated = repeatProjectedEvents(500);
    const firstVisible = repeated.at(-200);
    render(<StoryView events={repeated} onSelectEvent={vi.fn()} />);

    expect(screen.getAllByRole("listitem")).toHaveLength(200);
    expect(screen.getByText("Showing the latest 200 of 500 visible events.")).toBeVisible();
    expect(screen.getByText(`301 ${firstVisible?.label}`)).toBeVisible();
  });
});
