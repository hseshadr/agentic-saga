import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { projectReplay } from "../replay/project-replay";
import { loadTraceFixture } from "../test/load-trace-fixture";
import { parseRunTrace } from "../trace/parse-run-trace";
import { ProofView } from "./proof-view";

function projection(name: "business-failure" | "compensation-failure", cursor = 99) {
  const parsed = parseRunTrace(loadTraceFixture(name));
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectReplay(parsed.trace, cursor);
}

describe("ProofView", () => {
  it("shows exact recorded rule, source, version, result, and target", async () => {
    const user = userEvent.setup();
    const select = vi.fn();
    const replay = projection("business-failure");
    render(<ProofView onSelectEvent={select} projection={replay} />);

    expect(screen.getByText("3 of 3 recorded invariants valid")).toBeVisible();
    expect(screen.getByText("payment_refunded")).toBeVisible();
    expect(screen.getAllByText("ecommerce-invariants-v1")).toHaveLength(3);
    expect(screen.getAllByText("compensated_verified")).toHaveLength(3);
    await user.click(screen.getByRole("button", { name: "Inspect proof source event 36" }));
    expect(select).toHaveBeenCalledWith(replay.proof.sourceEvent?.event_id);
  });

  it("shows no future proof at an earlier replay position", () => {
    render(<ProofView onSelectEvent={vi.fn()} projection={projection("business-failure", 34)} />);
    expect(
      screen.getByText("No invariant proof is visible at this replay position."),
    ).toBeVisible();
  });

  it("distinguishes HUMAN_REQUIRED from a verified terminal result", () => {
    render(<ProofView onSelectEvent={vi.fn()} projection={projection("compensation-failure")} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Human review required. This recorded stop is quiescent, not terminal proof.",
    );
    expect(screen.queryByText(/recorded invariants valid/i)).not.toBeInTheDocument();
  });
});
