import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { projectReplay } from "../replay/project-replay";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { repeatProjectedEvents } from "../test/projected-events";
import type { ProjectedEvent } from "../trace/flight-projection";
import { parseRunTrace } from "../trace/parse-run-trace";
import { LedgerView } from "./ledger-view";

function events(): readonly ProjectedEvent[] {
  const parsed = parseRunTrace(loadTraceFixture());
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectReplay(parsed.trace, 99).flight.orderedEvents;
}

describe("LedgerView", () => {
  it("renders a semantic, filterable ledger and inspects exact evidence", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    render(<LedgerView events={events()} onSelectEvent={select} />);

    expect(screen.getByRole("table", { name: "Recorded ledger" })).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Lane"), "effect");
    await user.selectOptions(screen.getByLabelText("State"), "compensating");
    await user.type(screen.getByLabelText("Search recorded fields"), "refund_payment");
    const ledger = screen.getByRole("table", { name: "Recorded ledger" });
    expect(within(ledger).getAllByRole("row")).toHaveLength(4);
    await user.click(within(ledger).getByRole("button", { name: "Inspect event 28" }));
    expect(select).toHaveBeenCalledWith(events()[27]?.event.event_id);
  });

  it("paginates a large trace without rendering an unbounded ledger", async () => {
    const user = userEvent.setup();
    render(<LedgerView events={repeatProjectedEvents(500)} onSelectEvent={vi.fn()} />);

    expect(screen.getAllByRole("button", { name: /inspect event/i })).toHaveLength(25);
    expect(screen.getByText("Page 1 of 20")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Next ledger page" }));
    expect(screen.getAllByRole("button", { name: /inspect event/i })).toHaveLength(25);
    expect(screen.getByRole("button", { name: "Inspect event 26" })).toBeVisible();
  });

  it("states when visible replay evidence does not match", async () => {
    const user = userEvent.setup();
    render(<LedgerView events={events()} onSelectEvent={vi.fn()} />);
    await user.type(screen.getByLabelText("Search recorded fields"), "not-recorded-anywhere");
    expect(screen.getByText("No visible ledger events match these filters.")).toBeVisible();
  });
});
