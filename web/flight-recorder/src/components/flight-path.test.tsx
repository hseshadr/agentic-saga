import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { repeatedFlightProjection } from "../test/projected-events";
import { FlightPath } from "./flight-path";

describe("FlightPath", () => {
  it("bounds a large path while preserving the newest replay signals", () => {
    render(
      <FlightPath
        causalIds={new Set()}
        onSelect={vi.fn()}
        projection={repeatedFlightProjection(500)}
        selectedEventId="evt_0000000000000500"
      />,
    );

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(200);
    expect(screen.getByText("500 ledger events · showing latest 200")).toBeVisible();
    const first = screen.getByRole("button", { name: "301. Read observed" });
    const last = buttons.at(-1);
    if (!last) throw new Error("bounded path must include its last visible event");
    expect(first).toBeVisible();
    expect(first.closest("li")).toHaveStyle("--event-row: 1");
    expect(last.closest("li")).toHaveStyle("--event-row: 200");
  });
});
