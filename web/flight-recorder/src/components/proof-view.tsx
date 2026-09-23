import type { ReplayProjection } from "../replay/project-replay";
import type { TraceEvent, TraceProof } from "../trace/schema";
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
  const { recovery } = projection;
  if (!proof.sourceEvent && !recovery.record)
    return (
      <section aria-label="Proof view" className={styles.view}>
        <p>No invariant proof is visible at this replay position.</p>
      </section>
    );
  return (
    <section aria-label="Proof view" className={styles.view}>
      {recovery.record ? (
        <RecoveryRecord
          onSelectEvent={onSelectEvent}
          record={recovery.record}
          recovery={recovery}
        />
      ) : null}
      {proof.sourceEvent ? (
        <InvariantProof
          onSelectEvent={onSelectEvent}
          proof={proof}
          sourceEvent={proof.sourceEvent}
        />
      ) : null}
    </section>
  );
}

interface RecoveryRecordProps {
  readonly onSelectEvent: (eventId: string) => void;
  readonly record: TraceEvent;
  readonly recovery: ReplayProjection["recovery"];
}

function RecoveryRecord({ onSelectEvent, record, recovery }: RecoveryRecordProps) {
  const { compensatedOperationIds, confirmedOperationIds } = recovery;
  return (
    <>
      <h2>{`${confirmedOperationIds.length} of ${compensatedOperationIds.length} undo steps confirmed`}</h2>
      <p>
        The Workflow recorded that its journaled compensations ran in reverse order. This is a
        completion record, not an invariant proof: each undo step counts only with its own confirmed
        outcome.
      </p>
      <button onClick={() => onSelectEvent(record.event_id)} type="button">
        {`Inspect recovery record event ${record.saga_seq}`}
      </button>
    </>
  );
}

interface InvariantProofProps {
  readonly onSelectEvent: (eventId: string) => void;
  readonly proof: ReplayProjection["proof"];
  readonly sourceEvent: TraceEvent;
}

function InvariantProof({ onSelectEvent, proof, sourceEvent }: InvariantProofProps) {
  return (
    <>
      <h2>{`${proof.validRuleIds.length} of ${proof.expectedRuleIds.length} recorded invariants valid`}</h2>
      <ul aria-label="Recorded invariant proof" className={styles.proofList}>
        {proof.records.map((record) => (
          <ProofRecord key={`${record.source_event_id}:${record.rule_id}`} proof={record} />
        ))}
      </ul>
      <button onClick={() => onSelectEvent(sourceEvent.event_id)} type="button">
        {`Inspect proof source event ${sourceEvent.saga_seq}`}
      </button>
      {proof.missingRuleIds.length ? (
        <p>{`Missing recorded proof: ${proof.missingRuleIds.join(", ")}`}</p>
      ) : null}
    </>
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
