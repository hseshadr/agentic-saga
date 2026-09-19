import { useMemo, useState } from "react";
import { filterLedgerEvents, type LedgerFilter } from "../ledger/filter-events";
import { authorityLabel, type FlightLaneId, type ProjectedEvent } from "../trace/flight-projection";
import type { SagaStatus } from "../trace/schema";
import styles from "./inspection-views.module.css";

const PAGE_SIZE = 25;
const laneOptions: readonly (FlightLaneId | "all")[] = ["all", "agent", "guard", "effect", "proof"];

export interface LedgerViewProps {
  readonly events: readonly ProjectedEvent[];
  readonly onSelectEvent: (eventId: string) => void;
}

export function LedgerView({ events, onSelectEvent }: LedgerViewProps) {
  const [filter, setFilter] = useState<LedgerFilter>({ lane: "all", query: "", status: "all" });
  const [page, setPage] = useState(0);
  const filtered = useMemo(() => filterLedgerEvents(events, filter), [events, filter]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const visible = filtered.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
  const update = (change: Partial<LedgerFilter>) => {
    setFilter((current) => ({ ...current, ...change }));
    setPage(0);
  };
  return (
    <section aria-label="Ledger view" className={styles.view}>
      <LedgerFilters events={events} filter={filter} update={update} />
      <LedgerTable events={visible} onSelectEvent={onSelectEvent} />
      <nav aria-label="Ledger pages" className={styles.pagination}>
        <button disabled={safePage === 0} onClick={() => setPage(safePage - 1)} type="button">
          Previous ledger page
        </button>
        <span>{`Page ${safePage + 1} of ${pageCount}`}</span>
        <button
          disabled={safePage + 1 === pageCount}
          onClick={() => setPage(safePage + 1)}
          type="button"
        >
          Next ledger page
        </button>
      </nav>
    </section>
  );
}

interface FilterProps {
  readonly events: readonly ProjectedEvent[];
  readonly filter: LedgerFilter;
  readonly update: (change: Partial<LedgerFilter>) => void;
}

function LedgerFilters({ events, filter, update }: FilterProps) {
  const states = [...new Set(events.map(({ event }) => event.after_status))];
  return (
    <fieldset className={styles.filters}>
      <legend>Filter visible replay evidence</legend>
      <label>
        Lane
        <select value={filter.lane} onChange={(event) => update({ lane: laneValue(event) })}>
          {laneOptions.map((lane) => (
            <option key={lane} value={lane}>
              {lane}
            </option>
          ))}
        </select>
      </label>
      <label>
        State
        <select value={filter.status} onChange={(event) => update({ status: statusValue(event) })}>
          <option value="all">all</option>
          {states.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
      </label>
      <label>
        Search recorded fields
        <input
          maxLength={120}
          onChange={(event) => update({ query: event.currentTarget.value })}
          type="search"
          value={filter.query}
        />
      </label>
    </fieldset>
  );
}

function LedgerTable({ events, onSelectEvent }: LedgerViewProps) {
  return (
    <div className={styles.tableFrame}>
      <table aria-label="Recorded ledger" className={styles.ledger}>
        <thead>
          <tr>
            <th scope="col">Sequence</th>
            <th scope="col">Recorded</th>
            <th scope="col">Authority</th>
            <th scope="col">Event</th>
            <th scope="col">State</th>
            <th scope="col">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {events.length === 0 ? (
            <tr>
              <td colSpan={6}>No visible ledger events match these filters.</td>
            </tr>
          ) : (
            events.map((item) => (
              <LedgerRow item={item} key={item.event.event_id} onSelect={onSelectEvent} />
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function LedgerRow({
  item,
  onSelect,
}: {
  readonly item: ProjectedEvent;
  readonly onSelect: (id: string) => void;
}) {
  const event = item.event;
  return (
    <tr>
      <td data-label="Sequence">{event.saga_seq}</td>
      <td data-label="Recorded">{event.recorded_at}</td>
      <td data-label="Authority">{authorityLabel(event.authority)}</td>
      <td data-label="Event">{item.label}</td>
      <td data-label="State">{event.after_status}</td>
      <td data-label="Evidence">
        <button onClick={() => onSelect(event.event_id)} type="button">
          {`Inspect event ${event.saga_seq}`}
        </button>
      </td>
    </tr>
  );
}

function laneValue(event: React.ChangeEvent<HTMLSelectElement>): LedgerFilter["lane"] {
  return event.currentTarget.value as LedgerFilter["lane"];
}

function statusValue(event: React.ChangeEvent<HTMLSelectElement>): LedgerFilter["status"] {
  return event.currentTarget.value as SagaStatus | "all";
}
