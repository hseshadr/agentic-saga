import { projectReplay } from "../replay/project-replay";
import type { FlightProjection, ProjectedEvent } from "../trace/flight-projection";
import { parseRunTrace } from "../trace/parse-run-trace";
import { loadTraceFixture } from "./load-trace-fixture";

export function repeatProjectedEvents(count: number): readonly ProjectedEvent[] {
  const source = fixtureProjection().orderedEvents;
  return Array.from({ length: count }, (_, index) => repeatEvent(source, index));
}

export function repeatedFlightProjection(count: number): FlightProjection {
  const original = fixtureProjection();
  const orderedEvents = repeatProjectedEvents(count);
  const lanes = original.lanes.map((lane) => ({
    ...lane,
    events: orderedEvents.filter((item) => item.lane === lane.id),
  }));
  return { ...original, lanes, orderedEvents };
}

function fixtureProjection(): FlightProjection {
  const parsed = parseRunTrace(loadTraceFixture());
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectReplay(parsed.trace, 99).flight;
}

function repeatEvent(source: readonly ProjectedEvent[], index: number): ProjectedEvent {
  const item = source[index % source.length];
  if (!item) throw new Error("fixture must contain events");
  const sequence = index + 1;
  return {
    ...item,
    event: {
      ...item.event,
      event_id: `evt_${sequence.toString().padStart(16, "0")}`,
      saga_seq: sequence,
    },
  };
}
