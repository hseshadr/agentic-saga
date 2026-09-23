import type { ReplayProjection } from "../replay/project-replay";
import type { ScenarioIndexEntry } from "../scenarios/schema";
import styles from "./recorder-workbench.module.css";

export interface OutcomeHeaderProps {
  readonly entry: ScenarioIndexEntry;
  readonly projection: ReplayProjection;
}

export function OutcomeHeader({ entry, projection }: OutcomeHeaderProps) {
  return (
    <header className={styles.stateStrip}>
      <div className={styles.brand}>
        <h1>Agentic Saga Replay</h1>
        <p>{outcomeSummary(entry, projection.currentStatus)}</p>
      </div>
      <dl className={styles.runFacts}>
        <Fact label="Scenario" value={entry.name} />
        <Fact label="Recorded outcome" value={statusLabel(projection.currentStatus)} />
        <Fact label="Final safety checks" value={proofLabel(projection)} />
      </dl>
    </header>
  );
}

interface FactProps {
  readonly label: string;
  readonly value: string;
}

function Fact({ label, value }: FactProps) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function title(value: string): string {
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function proofLabel(projection: ReplayProjection): string {
  const { compensatedOperationIds, confirmedOperationIds, record } = projection.recovery;
  if (record)
    return `${confirmedOperationIds.length} / ${compensatedOperationIds.length} undo steps confirmed`;
  const { expectedRuleIds, validRuleIds } = projection.proof;
  const state = projection.terminalVerified ? "valid" : "visible";
  return `${validRuleIds.length} / ${expectedRuleIds.length} checks ${state}`;
}

function statusLabel(value: string): string {
  const labels: Readonly<Record<string, string>> = {
    compensated_verified: "Safely undone",
    human_required: "Needs human review",
    succeeded_verified: "Completed safely",
    running: "Recording ended before completion",
    compensating: "Recovery not completed in recording",
    aborted_clean: "Stopped without changes",
  };
  return labels[value] ?? title(value);
}

function outcomeSummary(entry: ScenarioIndexEntry, outcome: string): string {
  const subject = entry.presentation === "ecommerce" ? "Order" : "Workflow";
  if (outcome === "succeeded_verified") return `${subject} succeeded. Final state verified.`;
  if (outcome === "compensated_verified")
    return `${subject} failed. Recovery succeeded: all completed changes were safely undone.`;
  if (outcome === "human_required")
    return `${subject} did not complete. Recovery is unresolved and needs human review.`;
  if (outcome === "aborted_clean") return `${subject} stopped without leaving changes behind.`;
  return "This recording has no completed outcome. Replay does not execute the workflow.";
}
