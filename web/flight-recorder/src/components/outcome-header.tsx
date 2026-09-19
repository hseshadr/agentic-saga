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
        <p>Watch a recorded Saga unfold, one safe decision at a time.</p>
      </div>
      <dl className={styles.runFacts}>
        <Fact label="Scenario" value={entry.name} />
        <Fact label="Current state" value={statusLabel(projection.currentStatus)} />
        <Fact label="Safety checks" value={proofLabel(projection)} />
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
  const { expectedRuleIds, validRuleIds } = projection.proof;
  const state = projection.terminalVerified ? "valid" : "visible";
  return `${validRuleIds.length} / ${expectedRuleIds.length} checks ${state}`;
}

function statusLabel(value: string): string {
  const labels: Readonly<Record<string, string>> = {
    compensated_verified: "Safely undone",
    compensating: "Undoing completed work",
    human_required: "Waiting for a human",
    succeeded_verified: "Completed safely",
  };
  return labels[value] ?? title(value);
}
