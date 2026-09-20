import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./app";
import { parseScenarioIndex, ScenarioRepository } from "./scenarios/repository";
import { loadIndexFixture } from "./test/load-index-fixture";
import { loadTraceFixture, type TraceFixtureName } from "./test/load-trace-fixture";
import { parseRunTrace } from "./trace/parse-run-trace";

afterEach(() => vi.useRealTimers());

function trace(name: TraceFixtureName) {
  const result = parseRunTrace(loadTraceFixture(name));
  if (!result.ok) throw new Error("fixture must be valid");
  return result.trace;
}

function fixture() {
  vi.useFakeTimers();
  const parsed = parseScenarioIndex(loadIndexFixture());
  if (!parsed.ok) throw new Error("fixture must be valid");
  const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"));
  const loadIndex = vi
    .spyOn(repository, "loadIndex")
    .mockResolvedValue({ ok: true, value: parsed.value });
  const loadTrace = vi.spyOn(repository, "loadTrace").mockImplementation(async (entry) => ({
    ok: true,
    value: trace(entry.id as TraceFixtureName),
  }));
  return { repository, index: parsed.value, loadIndex, loadTrace };
}

describe("automatic recording refresh", () => {
  it("preserves replay position and selected scenario when the catalog is unchanged", async () => {
    const props = fixture();
    await act(async () => {
      render(<App repository={props.repository} />);
    });
    fireEvent.click(screen.getByRole("button", { name: /refund cannot be verified/i }));
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "Restart replay" }));
    const position = screen.getByText(/Event 1 of/).textContent;
    const callsBeforePolling = props.loadTrace.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });

    expect(screen.getByText(position ?? "missing")).toBeVisible();
    expect(screen.getByText("Recorded outcome").parentElement).toHaveTextContent(
      "Needs human review",
    );
    expect(props.loadTrace).toHaveBeenCalledTimes(callsBeforePolling);
    expect(screen.getByRole("status", { name: "Recording updates" })).toHaveTextContent(
      "Up to date",
    );
  });

  it("hides the previous trace while a changed digest loads and displays the new result", async () => {
    const props = fixture();
    await act(async () => {
      render(<App repository={props.repository} />);
    });
    let resolveTrace: ((value: { ok: true; value: ReturnType<typeof trace> }) => void) | undefined;
    props.loadTrace.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveTrace = resolve;
        }),
    );
    const changed = {
      ...props.index,
      runs: props.index.runs.map((entry, i) =>
        i === 0 ? { ...entry, trace_sha256: "a".repeat(64) } : entry,
      ),
    };
    props.loadIndex.mockResolvedValue({ ok: true, value: changed });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });

    expect(screen.getByText("Loading selected RunTrace…")).toBeVisible();
    expect(screen.queryByText("Completed safely")).not.toBeInTheDocument();
    await act(async () => {
      resolveTrace?.({ ok: true, value: trace("compensation-failure") });
    });
    expect(screen.getByText("Needs human review")).toBeVisible();
  });

  it("keeps the last recording on disconnect and recovers automatically", async () => {
    const props = fixture();
    await act(async () => {
      render(<App repository={props.repository} />);
    });
    const callsBeforePolling = props.loadTrace.mock.calls.length;
    props.loadIndex.mockResolvedValueOnce({
      ok: false,
      message: "Trace index could not be loaded.",
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(screen.getByText(/Reconnecting/)).toBeVisible();
    expect(screen.getByText("Completed safely")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(screen.getByText("Up to date")).toBeVisible();
    expect(props.loadTrace).toHaveBeenCalledTimes(callsBeforePolling);
  });

  it("retries unavailable trace data even when the catalog digest stays unchanged", async () => {
    const props = fixture();
    props.loadTrace.mockResolvedValueOnce({ ok: false, message: "RunTrace could not be loaded." });
    await act(async () => {
      render(<App repository={props.repository} />);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("RunTrace could not be loaded");
    const callsBeforeRetry = props.loadTrace.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(screen.getByText("Completed safely")).toBeVisible();
    expect(props.loadTrace).toHaveBeenCalledTimes(callsBeforeRetry + 1);
  });
});
