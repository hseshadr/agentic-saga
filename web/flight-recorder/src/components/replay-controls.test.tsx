import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ReplayControls } from "./replay-controls";

function actions() {
  return {
    next: vi.fn(),
    pause: vi.fn(),
    play: vi.fn(),
    previous: vi.fn(),
    restart: vi.fn(),
    seek: vi.fn(),
    watch: vi.fn(),
  };
}

describe("ReplayControls", () => {
  it("renders native replay controls and recorded position", () => {
    render(
      <ReplayControls
        actions={actions()}
        announce
        eventCount={3}
        reducedMotion={false}
        state={{ atEnd: false, atStart: true, cursor: 0, isPlaying: false }}
      />,
    );

    expect(screen.getByRole("button", { name: "Previous event" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Watch from start" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Play replay" })).toBeEnabled();
    expect(screen.getByText("Event 1 of 3")).toHaveAttribute("aria-live", "polite");
  });

  it("does not announce every timer tick during an active replay", () => {
    render(
      <ReplayControls
        actions={actions()}
        announce
        eventCount={3}
        reducedMotion={false}
        state={{ atEnd: false, atStart: false, cursor: 1, isPlaying: true }}
      />,
    );

    expect(screen.getByText("Event 2 of 3")).toHaveAttribute("aria-live", "off");
  });

  it("supports replay keys without stealing keys from form controls", async () => {
    const user = userEvent.setup();
    const callbacks = actions();
    render(
      <>
        <label htmlFor="query">Filter</label>
        <input id="query" />
        <ReplayControls
          actions={callbacks}
          announce={false}
          eventCount={3}
          reducedMotion={false}
          state={{ atEnd: false, atStart: false, cursor: 1, isPlaying: false }}
        />
      </>,
    );

    await user.keyboard("{ArrowRight}{ArrowLeft}{Home} ");
    expect(callbacks.next).toHaveBeenCalledOnce();
    expect(callbacks.previous).toHaveBeenCalledOnce();
    expect(callbacks.restart).toHaveBeenCalledOnce();
    expect(callbacks.play).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("textbox", { name: "Filter" }));
    await user.keyboard("{ArrowRight} ");
    expect(callbacks.next).toHaveBeenCalledOnce();
    expect(callbacks.play).toHaveBeenCalledOnce();
  });

  it("does not steal replay keys from any interactive control", async () => {
    const user = userEvent.setup();
    const callbacks = actions();
    render(
      <>
        <button type="button">A separate action</button>
        <ReplayControls
          actions={callbacks}
          announce={false}
          eventCount={3}
          reducedMotion={false}
          state={{ atEnd: false, atStart: false, cursor: 1, isPlaying: false }}
        />
      </>,
    );
    await user.click(screen.getByRole("button", { name: "A separate action" }));
    await user.keyboard("{ArrowRight}{Home}");
    expect(callbacks.next).not.toHaveBeenCalled();
    expect(callbacks.restart).not.toHaveBeenCalled();
  });

  it("uses Space to pause an active replay", async () => {
    const user = userEvent.setup();
    const callbacks = actions();
    render(
      <ReplayControls
        actions={callbacks}
        announce={false}
        eventCount={3}
        reducedMotion={false}
        state={{ atEnd: false, atStart: false, cursor: 1, isPlaying: true }}
      />,
    );
    await user.keyboard(" ");
    expect(callbacks.pause).toHaveBeenCalledOnce();
    expect(callbacks.play).not.toHaveBeenCalled();
  });

  it("explains reduced motion and keeps manual stepping available", () => {
    render(
      <ReplayControls
        actions={actions()}
        announce={false}
        eventCount={3}
        reducedMotion
        state={{ atEnd: false, atStart: true, cursor: 0, isPlaying: false }}
      />,
    );

    expect(screen.getByRole("button", { name: "Play replay" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Watch from start" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next event" })).toBeEnabled();
    expect(screen.getByText(/motion preference/i)).toBeVisible();
  });
});
