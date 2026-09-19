import type { Ref } from "react";
import { authorityLabel } from "../trace/flight-projection";
import { stringifyTraceJson } from "../trace/lossless-json";
import type { JsonObject, TraceEvent } from "../trace/schema";
import styles from "./inspection-views.module.css";

export interface EventDetailsProps {
  readonly className?: string | undefined;
  readonly event: TraceEvent | undefined;
  readonly onReturnFocus?: () => void;
  readonly ref?: Ref<HTMLElement>;
}

export function EventDetails({ className, event, onReturnFocus, ref }: EventDetailsProps) {
  if (!event)
    return <aside aria-label="Recorded evidence" className={className} ref={ref} tabIndex={-1} />;
  return (
    <aside aria-label="Recorded evidence" className={className} ref={ref} tabIndex={-1}>
      <h2>Recorded evidence</h2>
      <p className={styles.inspectorTitle}>{event.event_type.replaceAll("_", " ")}</p>
      {onReturnFocus ? (
        <button className={styles.evidenceReturn} onClick={onReturnFocus} type="button">
          Return to selected event
        </button>
      ) : null}
      <dl className={styles.evidenceFacts}>
        <Fact label="Ledger sequence" value={String(event.saga_seq)} />
        <Fact label="Authority" value={authorityLabel(event.authority)} />
        <Fact label="Actor" value={event.actor} />
        <Fact label="Before state" value={event.before_status} />
        <Fact label="After state" value={event.after_status} />
        <Fact label="Attempt" value={stringValue(event.attempt)} />
        <Fact label="Operation" value={event.operation_id} />
        <Fact label="Compensates operation" value={event.compensates_operation_id} />
        <Fact label="Input hash" value={event.input_hash} />
        <Fact label="Output hash" value={event.output_hash} />
      </dl>
      <Evidence label="Redacted input" value={event.redacted_input} />
      <Evidence label="Redacted output" value={event.redacted_output} />
      <Evidence label="Policy decision" value={event.policy_decision} />
      <Evidence label="Receipt" value={event.receipt} />
      <Evidence label="Structured rationale" value={event.rationale} />
    </aside>
  );
}

interface FactProps {
  readonly label: string;
  readonly value: string | null;
}

function Fact({ label, value }: FactProps) {
  if (value === null) return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

interface EvidenceProps {
  readonly label: string;
  readonly value: JsonObject | null;
}

function Evidence({ label, value }: EvidenceProps) {
  if (value === null) return null;
  return (
    <section className={styles.evidenceBlock}>
      <h3>{label}</h3>
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: focus exposes horizontally scrollable evidence to keyboard users. */}
      <pre tabIndex={0}>{stringifyTraceJson(value, 2)}</pre>
    </section>
  );
}

function stringValue(value: TraceEvent["attempt"]): string | null {
  return value === null ? null : String(value);
}
