import type { ReplayProjection } from "../replay/project-replay";
import type { ScenarioIndexEntry } from "../scenarios/schema";
import type { RunTrace } from "../trace/schema";
import styles from "./recorder-workbench.module.css";

export interface OutcomeHeaderProps {
  readonly entry: ScenarioIndexEntry;
  readonly projection: ReplayProjection;
  readonly trace: RunTrace;
}

export function OutcomeHeader({ entry, projection, trace }: OutcomeHeaderProps) {
  return (
    <header className={styles.stateStrip}>
      <div className={styles.brand}>
        <h1>Saga Flight Recorder</h1>
        <p>Read-only causal evidence</p>
      </div>
      <dl className={styles.runFacts}>
        <Fact label="Run" mono value={shortId(trace.run_id)} />
        <Fact label="Mode" value={title(entry.mode)} />
        <Fact label="Outcome" value={title(projection.currentStatus)} />
        <Fact label="Proof" value={proofLabel(projection)} />
      </dl>
    </header>
  );
}

interface FactProps {
  readonly label: string;
  readonly mono?: boolean;
  readonly value: string;
}

function Fact({ label, mono = false, value }: FactProps) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono ? styles.mono : undefined}>{value}</dd>
    </div>
  );
}

function title(value: string): string {
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function shortId(value: string): string {
  return `${value.slice(0, 14)}…${value.slice(-6)}`;
}

function proofLabel(projection: ReplayProjection): string {
  const { expectedRuleIds, validRuleIds } = projection.proof;
  const state = projection.terminalVerified ? "valid" : "visible";
  return `${validRuleIds.length} / ${expectedRuleIds.length} proofs ${state}`;
}
