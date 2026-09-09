import type { RunTrace, TraceEvent, TraceProof } from "./schema";

export type FlightLaneId = "agent" | "guard" | "effect" | "proof";
export type SignalKind = "agent" | "guard" | "effect" | "compensation" | "proof" | "fault";

export interface ProjectedEvent {
  readonly event: TraceEvent;
  readonly label: string;
  readonly lane: FlightLaneId;
  readonly laneLabel: string;
  readonly signal: SignalKind;
}

export interface FlightLane {
  readonly events: readonly ProjectedEvent[];
  readonly id: FlightLaneId;
  readonly label: string;
}

export interface FlightProjection {
  readonly lanes: readonly FlightLane[];
  readonly orderedEvents: readonly ProjectedEvent[];
  readonly proofs: readonly TraceProof[];
}

const laneLabels: Readonly<Record<FlightLaneId, string>> = {
  agent: "Agent",
  effect: "Effect + repair",
  guard: "Kernel guard",
  proof: "Proof",
};

export function projectFlight(trace: RunTrace): FlightProjection {
  const orderedEvents = trace.events.map(projectEvent);
  const laneIds: FlightLaneId[] = ["agent", "guard", "effect", "proof"];
  const lanes = laneIds.map((id) => ({
    events: orderedEvents.filter((item) => item.lane === id),
    id,
    label: laneLabels[id],
  }));
  return { lanes, orderedEvents, proofs: trace.proofs };
}

export function causalEventIds(projection: FlightProjection, selectedId: string): Set<string> {
  const selected = projection.orderedEvents.find((item) => item.event.event_id === selectedId);
  const eventIds = new Set(selected ? [selectedId] : []);
  const operationIds = operationSet(selected?.event);
  if (operationIds.size === 0) return eventIds;
  let changed = true;
  while (changed) changed = extendCausalSet(projection, operationIds, eventIds);
  return eventIds;
}

function extendCausalSet(
  projection: FlightProjection,
  operationIds: Set<string>,
  eventIds: Set<string>,
): boolean {
  let changed = false;
  for (const item of projection.orderedEvents) {
    if (!touchesOperation(item.event, operationIds)) continue;
    changed = addIfPresent(operationIds, item.event.operation_id) || changed;
    changed = addIfPresent(operationIds, item.event.compensates_operation_id) || changed;
    eventIds.add(item.event.event_id);
  }
  return changed;
}

function operationSet(event: TraceEvent | undefined): Set<string> {
  const ids = new Set<string>();
  addIfPresent(ids, event?.operation_id);
  addIfPresent(ids, event?.compensates_operation_id);
  return ids;
}

function touchesOperation(event: TraceEvent, operationIds: Set<string>): boolean {
  return Boolean(
    (event.operation_id && operationIds.has(event.operation_id)) ||
      (event.compensates_operation_id && operationIds.has(event.compensates_operation_id)),
  );
}

function addIfPresent(values: Set<string>, value: string | null | undefined): boolean {
  if (!value || values.has(value)) return false;
  values.add(value);
  return true;
}

function projectEvent(event: TraceEvent): ProjectedEvent {
  const lane = laneFor(event);
  return {
    event,
    label: displayLabel(event.event_type),
    lane,
    laneLabel: laneLabels[lane],
    signal: signalFor(event),
  };
}

function laneFor(event: TraceEvent): FlightLaneId {
  if (event.authority === "agent") return "agent";
  if (event.authority === "effect" || event.authority === "compensation") return "effect";
  if (event.authority === "proof" || event.authority === "human") return "proof";
  return "guard";
}

function signalFor(event: TraceEvent): SignalKind {
  if (event.authority === "human") return "fault";
  if (event.authority === "compensation" || event.direction === "compensation")
    return "compensation";
  return laneFor(event);
}

function displayLabel(value: string): string {
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}
