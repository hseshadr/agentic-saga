import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { parseScenarioIndex } from "../scenarios/repository";
import { loadIndexFixture } from "../test/load-index-fixture";
import { loadTraceFixture, type TraceFixtureName } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import { RecorderWorkbench } from "./recorder-workbench";

afterEach(() => vi.useRealTimers());

function fixtureProps(name: TraceFixtureName = "business-failure") {
  const index = parseScenarioIndex(loadIndexFixture());
  const trace = parseRunTrace(loadTraceFixture(name));
  if (!index.ok || !trace.ok) throw new Error("real fixture must satisfy browser contracts");
  const entry = index.value.runs.find(({ id }) => id === name);
  if (!entry) throw new Error("real fixture must contain one indexed run");
  return { entry, index: index.value, trace: trace.trace };
}

describe("RecorderWorkbench", () => {
  it.each([
    ["happy-path", "Order succeeded. Final state verified.", "Completed safely"],
    ["lost-response", "Order succeeded. Final state verified.", "Completed safely"],
    [
      "compensation-failure",
      "Order did not complete. Recovery is unresolved and needs human review.",
      "Needs human review",
    ],
  ] as const)("explains the recorded business outcome for %s", (name, summary, outcome) => {
    render(<RecorderWorkbench {...fixtureProps(name)} onSelectRun={vi.fn()} />);

    expect(screen.getByText(summary)).toBeVisible();
    expect(screen.getByText("Recorded outcome").parentElement).toHaveTextContent(outcome);
    expect(screen.getByText("Replay complete")).toBeVisible();
  });

  it("identifies live-agent recordings without claiming a provider the catalog does not record", () => {
    const props = fixtureProps();
    render(
      <RecorderWorkbench
        {...props}
        entry={{ ...props.entry, mode: "live" }}
        onSelectRun={vi.fn()}
      />,
    );

    expect(screen.getByText(/Recording from a live agent run/)).toBeVisible();
    expect(screen.getByText(/Model provider is not recorded/)).toBeVisible();
    expect(screen.queryByText(/JEV is not used/)).not.toBeInTheDocument();
  });

  it("does not invent model provenance for imported recordings", () => {
    const props = fixtureProps();
    const entry = { ...props.entry, mode: "unknown" as const };
    render(
      <RecorderWorkbench
        {...props}
        entry={entry}
        index={{ ...props.index, runs: [entry] }}
        onSelectRun={vi.fn()}
      />,
    );

    expect(screen.getByText(/Agent provenance not recorded\. Model use/)).toBeVisible();
    expect(screen.getByText("Agent provenance not recorded")).toBeVisible();
    expect(screen.queryByText(/JEV is not used|Scripted recording|No live model calls/)).toBeNull();
  });

  it("renders a useful signal board from real compensation evidence", () => {
    render(<RecorderWorkbench {...fixtureProps()} onSelectRun={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Agentic Saga Replay" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /one order/i })).toBeInTheDocument();
    expect(screen.getByText("Every completed change was safely undone")).toBeVisible();
    expect(
      within(screen.getByRole("region", { name: /one order/i })).getByText("Refund payment"),
    ).toBeVisible();
    expect(screen.getByText("Recorded outcome").parentElement).toHaveTextContent("Safely undone");
    expect(screen.getByText(/Order failed. Recovery succeeded/)).toBeVisible();
    expect(screen.getByText(/JEV is not used/)).toBeVisible();
    expect(screen.getByText("Replay complete")).toBeVisible();
    expect(screen.getByText("3 / 3 undo steps confirmed")).toBeInTheDocument();
    const events = screen.getByRole("list", { name: "Ledger events in causal order" });
    const signals = within(events).getAllByRole("button");
    expect(signals.slice(0, 4).map((signal) => signal.getAttribute("aria-label"))).toEqual([
      expect.stringMatching(/^1\./),
      expect.stringMatching(/^2\./),
      expect.stringMatching(/^3\./),
      expect.stringMatching(/^4\./),
    ]);
    expect(screen.getByRole("heading", { name: /Effect \+ repair/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Workflow guard/ })).toBeInTheDocument();
    expect(screen.queryByText(/kernel/i)).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Story" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("table", { name: "Causal story" })).toBeVisible();
  });

  it("keeps the recorded outcome stable while replaying historical evidence", async () => {
    const user = userEvent.setup();
    render(<RecorderWorkbench {...fixtureProps()} onSelectRun={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Restart replay" }));

    expect(screen.getByText("Recorded outcome").parentElement).toHaveTextContent("Safely undone");
    expect(screen.getByText("3 / 3 undo steps confirmed")).toBeVisible();
    expect(screen.getByText("Replay paused")).toBeVisible();
    expect(screen.queryByText("Running")).not.toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Ledger events in causal order" })).toHaveTextContent(
      "Saga started",
    );
    expect(screen.getByRole("table", { name: "Causal story" })).toHaveTextContent("Saga started");
    expect(screen.queryByText("Compensated verified")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show outcome" }));
    expect(screen.getByText("Replay complete")).toBeVisible();
    expect(screen.getByText("Every completed change was safely undone")).toBeVisible();
    expect(screen.getByRole("button", { name: "Show outcome" })).toBeDisabled();
  });

  it("preserves selected recorded evidence across local views", async () => {
    const user = userEvent.setup();
    const props = fixtureProps();
    render(<RecorderWorkbench {...props} onSelectRun={vi.fn()} />);
    const compensation = props.trace.events.find(
      ({ event_type }) => event_type === "compensation_outcome_recorded",
    );
    if (!compensation) throw new Error("fixture must include compensation evidence");
    const signal = screen.getByRole("button", {
      name: new RegExp(`^${compensation.saga_seq}\\. Compensation outcome recorded`),
    });
    await user.click(signal);

    await user.click(screen.getByRole("tab", { name: "Ledger" }));
    expect(screen.getByRole("complementary", { name: "Recorded evidence" })).toHaveTextContent(
      `Ledger sequence${compensation.saga_seq}`,
    );
    await user.click(screen.getByRole("tab", { name: "Proof" }));
    expect(screen.getByRole("complementary", { name: "Recorded evidence" })).toHaveTextContent(
      `Ledger sequence${compensation.saga_seq}`,
    );
  });

  it("keeps every tab target mounted and preserves local ledger filters", async () => {
    const user = userEvent.setup();
    render(<RecorderWorkbench {...fixtureProps()} onSelectRun={vi.fn()} />);

    for (const view of ["story", "ledger", "proof"]) {
      const tab = screen.getByRole("tab", { name: new RegExp(view, "i") });
      expect(
        document.getElementById(tab.getAttribute("aria-controls") ?? "missing"),
      ).not.toBeNull();
    }
    await user.click(screen.getByRole("tab", { name: "Ledger" }));
    await user.type(screen.getByLabelText("Search recorded fields"), "refund_payment");
    await user.click(screen.getByRole("tab", { name: "Proof" }));
    await user.click(screen.getByRole("tab", { name: "Ledger" }));
    expect(screen.getByLabelText("Search recorded fields")).toHaveValue("refund_payment");
  });

  it("moves focus to recorded evidence after an explicit inspect action", async () => {
    const user = userEvent.setup();
    const props = fixtureProps();
    render(<RecorderWorkbench {...props} onSelectRun={vi.fn()} />);

    await user.click(screen.getByRole("tab", { name: "Ledger" }));
    const compensation = props.trace.events.find(
      ({ event_type }) => event_type === "compensation_started",
    );
    if (!compensation) throw new Error("fixture must include compensation evidence");
    const inspect = screen.getByRole("button", {
      name: `Inspect event ${compensation.saga_seq}`,
    });
    await user.click(inspect);

    expect(screen.getByRole("complementary", { name: "Recorded evidence" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Return to selected event" }));
    expect(inspect).toHaveFocus();
  });

  it("pauses replay when the operator changes evidence views", () => {
    vi.useFakeTimers();
    const props = fixtureProps();
    render(<RecorderWorkbench {...props} onSelectRun={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Restart replay" }));
    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    fireEvent.click(screen.getByRole("tab", { name: "Ledger" }));
    act(() => vi.advanceTimersByTime(1_000));

    expect(screen.getByText(`Event 1 of ${props.trace.events.length}`)).toBeVisible();
    expect(screen.getByRole("button", { name: "Play replay" })).toBeVisible();
  });

  it("lets a keyboard user isolate a recorded forward and compensation chain", async () => {
    const user = userEvent.setup();
    render(<RecorderWorkbench {...fixtureProps()} onSelectRun={vi.fn()} />);
    const repair = screen.getAllByRole("button", { name: /compensation outcome recorded/i })[0];
    if (!repair) throw new Error("fixture must include compensation evidence");

    await user.click(repair);

    expect(repair).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("complementary", { name: "Recorded evidence" })).toHaveTextContent(
      "Compensates operation",
    );
    const board = screen.getByRole("region", { name: "Causal flight path" });
    expect(within(board).getAllByTestId("causal-signal").length).toBeGreaterThanOrEqual(2);
    expect(within(board).getAllByTestId("muted-signal").length).toBeGreaterThan(1);
    const unrelated = within(board).getAllByTestId("muted-signal")[0];
    expect(unrelated).toHaveAccessibleName(/outside selected chain/i);
    expect(unrelated).toHaveTextContent("other");
  });

  it("exposes generic indexed runs without hard-coded scenario choices", async () => {
    const user = userEvent.setup();
    const onSelectRun = vi.fn();
    render(<RecorderWorkbench {...fixtureProps()} onSelectRun={onSelectRun} />);

    await user.click(screen.getByRole("button", { name: /refund cannot be verified/i }));

    expect(onSelectRun).toHaveBeenCalledWith("compensation-failure");
  });

  it("keeps the ecommerce presentation out of generic recorder catalogs", () => {
    const props = fixtureProps();
    render(
      <RecorderWorkbench
        {...props}
        entry={{ ...props.entry, presentation: undefined }}
        onSelectRun={vi.fn()}
      />,
    );

    expect(screen.queryByRole("region", { name: /one order/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/same order goal/i)).not.toBeInTheDocument();
  });

  it("has no detectable accessibility violations in every evidence view", async () => {
    const user = userEvent.setup();
    const { container } = render(<RecorderWorkbench {...fixtureProps()} onSelectRun={vi.fn()} />);

    for (const view of ["Story", "Ledger", "Proof"]) {
      await user.click(screen.getByRole("tab", { name: view }));
      const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
      expect(result.violations).toEqual([]);
    }
  }, 15_000);
});
