import type { CSSProperties } from "react";
import type { FlightProjection, ProjectedEvent } from "../trace/flight-projection";
import styles from "./recorder-workbench.module.css";

export interface FlightPathProps {
  readonly causalIds: ReadonlySet<string>;
  readonly onSelect: (eventId: string) => void;
  readonly projection: FlightProjection;
  readonly selectedEventId: string;
}

const maximumSignals = 200;

export function FlightPath({ causalIds, onSelect, projection, selectedEventId }: FlightPathProps) {
  const isolating = causalIds.size > 1;
  const signals = projection.orderedEvents.slice(-maximumSignals);
  const bounded = signals.length < projection.orderedEvents.length;
  return (
    <section aria-label="Causal flight path" className={styles.board}>
      <div className={styles.boardHeading}>
        <div>
          <h2>Causal flight path</h2>
          <p>Select a signal to isolate its recorded operation chain.</p>
        </div>
        <span>
          {projection.orderedEvents.length} ledger events
          {bounded ? ` · showing latest ${signals.length}` : ""}
        </span>
      </div>
      <div className={styles.lanes}>
        {projection.lanes.map((lane) => (
          <h3 className={styles.laneHeading} key={lane.id}>
            {lane.label} <span>{lane.events.length}</span>
          </h3>
        ))}
        <ol aria-label="Ledger events in causal order" className={styles.laneEvents}>
          {signals.map((item, index) => (
            <Signal
              causal={!isolating || causalIds.has(item.event.event_id)}
              isolating={isolating}
              item={item}
              key={item.event.event_id}
              onSelect={onSelect}
              row={index + 1}
              selected={selectedEventId === item.event.event_id}
            />
          ))}
        </ol>
      </div>
    </section>
  );
}

interface SignalProps {
  readonly causal: boolean;
  readonly isolating: boolean;
  readonly item: ProjectedEvent;
  readonly onSelect: (eventId: string) => void;
  readonly row: number;
  readonly selected: boolean;
}

function Signal({ causal, isolating, item, onSelect, row, selected }: SignalProps) {
  const marker = item.signal === "compensation" ? "↶" : "●";
  const position = {
    "--event-column": laneColumn(item.lane),
    "--event-row": row,
  } as CSSProperties;
  const membership = causal ? "Related selected chain" : "Outside selected chain";
  const label = `${item.event.saga_seq}. ${item.label}${isolating ? `. ${membership}` : ""}`;
  return (
    <li className={styles.signalRow} style={position}>
      <button
        aria-label={label}
        aria-pressed={selected}
        className={styles.signal}
        data-causal={causal}
        data-signal={item.signal}
        data-testid={causal ? "causal-signal" : "muted-signal"}
        onClick={() => onSelect(item.event.event_id)}
        type="button"
      >
        <span aria-hidden="true" className={styles.signalMarker}>
          {marker}
        </span>
        <span className={styles.sequence}>{String(item.event.saga_seq).padStart(2, "0")}</span>
        <span className={styles.mobileLane}>{item.laneLabel}</span>
        <span className={styles.signalLabel}>{item.label}</span>
        {isolating ? (
          <span className={styles.causalState}>{causal ? "chain" : "other"}</span>
        ) : null}
      </button>
    </li>
  );
}

function laneColumn(lane: ProjectedEvent["lane"]): number {
  return { agent: 1, effect: 3, guard: 2, proof: 4 }[lane];
}
