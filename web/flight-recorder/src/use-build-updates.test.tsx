import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { documentBuildVersion, ScenarioRepository } from "./scenarios/repository";
import { useBuildUpdates } from "./use-build-updates";

afterEach(() => vi.useRealTimers());

describe("build updates", () => {
  it("reloads only after a changed build, retrying checks after an unavailable server", async () => {
    vi.useFakeTimers();
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"));
    const load = vi
      .spyOn(repository, "loadBuildVersion")
      .mockResolvedValueOnce({ ok: true, value: documentBuildVersion(document) })
      .mockResolvedValueOnce({ ok: false, message: "unavailable" })
      .mockResolvedValue({ ok: true, value: '["/assets/new-build.js"]' });
    const reload = vi.fn();
    const hook = renderHook(() => useBuildUpdates(repository, reload));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });
    expect(reload).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(reload).toHaveBeenCalledOnce();
    hook.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("reads module and stylesheet build references without cache", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(
          '<html><head><script type="module" src="/assets/app-2.js"></script><link rel="stylesheet" href="/assets/app-2.css"></head></html>',
          { headers: { "content-type": "text/html" } },
        ),
      );
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    await expect(repository.loadBuildVersion()).resolves.toEqual({
      ok: true,
      value: '["/assets/app-2.css","/assets/app-2.js"]',
    });
    expect(fetcher).toHaveBeenCalledWith(new URL("https://recorder.test/"), { cache: "no-store" });
  });
});
