import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ViewTabs } from "./view-tabs";

describe("ViewTabs", () => {
  it("exposes one selected view with roving keyboard focus", async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<ViewTabs active="story" onChange={change} />);

    const story = screen.getByRole("tab", { name: "Story" });
    expect(story).toHaveAttribute("aria-selected", "true");
    story.focus();
    await user.keyboard("{ArrowRight}");
    expect(change).toHaveBeenCalledWith("ledger");
    expect(screen.getByRole("tab", { name: "Ledger" })).toHaveFocus();
  });

  it("moves Home and End to the first and last view", async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<ViewTabs active="ledger" onChange={change} />);
    const ledger = screen.getByRole("tab", { name: "Ledger" });
    ledger.focus();
    await user.keyboard("{End}{Home}");
    expect(change.mock.calls).toEqual([["proof"], ["story"]]);
  });
});
