import type { FlightLaneId, ProjectedEvent } from "../trace/flight-projection";
import type { JsonObject, JsonValue, SagaStatus } from "../trace/schema";

export interface LedgerFilter {
  readonly lane: FlightLaneId | "all";
  readonly query: string;
  readonly status: SagaStatus | "all";
}

export function filterLedgerEvents(
  events: readonly ProjectedEvent[],
  filter: LedgerFilter,
): readonly ProjectedEvent[] {
  const query = filter.query.trim().toLocaleLowerCase();
  return events.filter(
    (item) =>
      matches(filter.lane, item.lane) &&
      matches(filter.status, item.event.after_status) &&
      searchableFields(item).some((value) => value.toLocaleLowerCase().includes(query)),
  );
}

function matches(filter: string, value: string): boolean {
  return filter === "all" || filter === value;
}

function searchableFields(item: ProjectedEvent): readonly string[] {
  const event = item.event;
  return [
    item.label,
    event.event_type,
    event.tool_name,
    event.step_instance_id,
    event.operation_id,
    event.compensates_operation_id,
    event.correlation,
    event.input_hash,
    event.output_hash,
    ...receiptReferences(event.receipt),
  ].filter((value): value is string => typeof value === "string");
}

function receiptReferences(receipt: JsonObject | null): readonly (string | null)[] {
  if (!receipt) return [];
  return [
    receiptReference(receipt),
    receiptReference(receipt.receipt),
    ...receiptList(receipt.forward_receipts),
  ];
}

function receiptList(value: JsonValue | undefined): readonly (string | null)[] {
  return Array.isArray(value) ? value.map(receiptReference) : [];
}

function receiptReference(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("receipt_ref" in value)) return null;
  const reference = value.receipt_ref;
  return typeof reference === "string" ? reference : null;
}
