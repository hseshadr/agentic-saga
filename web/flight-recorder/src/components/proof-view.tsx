import type { ReplayProjection } from "../replay/project-replay";
import type { TraceProof } from "../trace/schema";
import styles from "./inspection-views.module.css";

export interface ProofViewProps {
  readonly onSelectEvent: (eventId: string) => void;
  readonly projection: ReplayProjection;
}

export function ProofView({ onSelectEvent, projection }: ProofViewProps) {
  const { proof } = projection;
  if (proof.humanRequired)
    return (
      <section aria-label="Proof view" className={styles.view}>
        <p className={styles.humanStop} role="status">
          Human review required. This recorded stop is quiescent, not terminal proof.
        </p>
      </section>
    );
  if (!proof.sourceEvent)
    return (
      <section aria-label="Proof view" className={styles.view}>
        <p>No invariant proof is visible at this replay position.</p>
      </section>
    );
  return (
    <section aria-label="Proof view" className={styles.view}>
      <h2>{`${proof.validRuleIds.length} of ${proof.expectedRuleIds.length} recorded invariants valid`}</h2>
      <ul aria-label="Recorded invariant proof" className={styles.proofList}>
        {proof.records.map((record) => (
          <ProofRecord key={`${record.source_event_id}:${record.rule_id}`} proof={record} />
        ))}
      </ul>
      <button onClick={() => onSelectEvent(proof.sourceEvent?.event_id ?? "")} type="button">
        {`Inspect proof source event ${proof.sourceEvent.saga_seq}`}
      </button>
      {proof.missingRuleIds.length ? (
        <p>{`Missing recorded proof: ${proof.missingRuleIds.join(", ")}`}</p>
      ) : null}
    </section>
  );
}

function ProofRecord({ proof }: { readonly proof: TraceProof }) {
  const marker = proof.result === "valid" ? "✓" : "!";
  return (
    <li className={styles.proofRecord} data-result={proof.result}>
      <strong>{proof.rule_id}</strong>
      <span className={styles.proofResult}>{`${marker} ${proof.result}`}</span>
      <dl>
        <Fact label="Invariant version" value={proof.invariant_version} />
        <Fact label="Source event" value={`${proof.source_event_seq} · ${proof.source_event_id}`} />
        <Fact label="Target state" value={proof.target_status} />
      </dl>
    </li>
  );
}

function Fact({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
