import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useReducedMotion } from "./use-reduced-motion";

afterEach(() => vi.unstubAllGlobals());

describe("useReducedMotion", () => {
  it("tracks the browser motion preference and unsubscribes on unmount", () => {
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    const query = {
      addEventListener: (_name: string, listener: (event: MediaQueryListEvent) => void) =>
        listeners.add(listener),
      matches: false,
      removeEventListener: (_name: string, listener: (event: MediaQueryListEvent) => void) =>
        listeners.delete(listener),
    } as unknown as MediaQueryList;
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => query),
    );
    const { result, unmount } = renderHook(useReducedMotion);

    expect(result.current).toBe(false);
    act(() => {
      for (const listener of listeners) listener({ matches: true } as MediaQueryListEvent);
    });
    expect(result.current).toBe(true);
    unmount();
    expect(listeners.size).toBe(0);
  });
});
