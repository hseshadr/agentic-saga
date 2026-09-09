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
    render(<StoryView events={projection().flight.orderedEvents} onSelectEvent={vi.fn()} />);

    const story = screen.getByRole("list", { name: "Causal story" });
    const items = within(story).getAllByRole("listitem");
    expect(items).toHaveLength(37);
    expect(items[0]).toHaveTextContent("01 Saga created");
    expect(screen.getByText("Kernel entered deterministic compensation.")).toBeVisible();
    expect(story).not.toHaveTextContent(/chain.of.thought|AI reasoning/i);
  });

  it("selects the exact recorded event for inspection", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    const projected = projection();
    render(<StoryView events={projected.flight.orderedEvents} onSelectEvent={select} />);

    await user.click(screen.getByRole("button", { name: /inspect story event 22/i }));

    expect(select).toHaveBeenCalledWith(projected.events[21]?.event_id);
  });

  it("bounds a large story while preserving the newest replay evidence", () => {
    render(<StoryView events={repeatProjectedEvents(500)} onSelectEvent={vi.fn()} />);

    expect(screen.getAllByRole("listitem")).toHaveLength(200);
    expect(screen.getByText("Showing the latest 200 of 500 visible events.")).toBeVisible();
    expect(screen.getByText("301 Read observed")).toBeVisible();
  });
});
