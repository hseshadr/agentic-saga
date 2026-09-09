import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useReplay } from "./use-replay";

afterEach(() => vi.useRealTimers());

describe("useReplay", () => {
  it("opens at the recorded outcome without autoplaying", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useReplay({ eventCount: 3, initialCursor: "end", pauseAfter: new Set() }),
    );

    act(() => vi.advanceTimersByTime(2_000));
    expect(result.current.state).toEqual({
      atEnd: true,
      atStart: false,
      cursor: 2,
      isPlaying: false,
    });
  });

  it("advances one recorded event and pauses at a semantic waypoint", () => {
    vi.useFakeTimers();
    const pauseAfter = new Set([1]);
    const { result } = renderHook(() => useReplay({ eventCount: 3, intervalMs: 700, pauseAfter }));

    act(() => result.current.actions.play());
    act(() => vi.advanceTimersByTime(700));
    expect(result.current.state).toMatchObject({ cursor: 1, isPlaying: false });
  });

  it("never starts a timer under reduced motion and cleans up an active timer", () => {
    vi.useFakeTimers();
    const { result, unmount } = renderHook(() =>
      useReplay({ eventCount: 3, pauseAfter: new Set(), reducedMotion: true }),
    );

    act(() => result.current.actions.play());
    expect(result.current.state.isPlaying).toBe(false);
    expect(vi.getTimerCount()).toBe(0);

    const active = renderHook(() => useReplay({ eventCount: 3, pauseAfter: new Set() }));
    act(() => active.result.current.actions.play());
    expect(vi.getTimerCount()).toBe(1);
    active.unmount();
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("clamps manual seeks and stops playback", () => {
    const { result } = renderHook(() => useReplay({ eventCount: 3, pauseAfter: new Set() }));

    act(() => result.current.actions.seek(99));
    expect(result.current.state).toMatchObject({ cursor: 2, isPlaying: false });
    act(() => result.current.actions.restart());
    expect(result.current.state).toMatchObject({ atStart: true, cursor: 0 });
  });
});
