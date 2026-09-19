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

    expect(screen.getByText("1 of 1 recorded invariants valid")).toBeVisible();
    expect(screen.getByText("obligations_reversed")).toBeVisible();
    expect(screen.getByText("temporal-compensation-proof-v1")).toBeVisible();
    expect(screen.getByText("compensated_verified")).toBeVisible();
    const sourceSequence = replay.proof.sourceEvent?.saga_seq;
    await user.click(
      screen.getByRole("button", { name: `Inspect proof source event ${sourceSequence}` }),
    );
    expect(select).toHaveBeenCalledWith(replay.proof.sourceEvent?.event_id);
  });

  it("shows no future proof at an earlier replay position", () => {
    const replay = projection("business-failure");
    const firstInvariant = replay.events.findIndex(
      ({ event_type }) => event_type === "invariant_evaluated",
    );
    render(
      <ProofView
        onSelectEvent={vi.fn()}
        projection={projection("business-failure", firstInvariant - 1)}
      />,
    );
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
