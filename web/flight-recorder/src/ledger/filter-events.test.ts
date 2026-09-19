import { describe, expect, it } from "vitest";
import { projectReplay } from "../replay/project-replay";
import { loadTraceFixture } from "../test/load-trace-fixture";
import type { ProjectedEvent } from "../trace/flight-projection";
import { parseRunTrace } from "../trace/parse-run-trace";
import { filterLedgerEvents } from "./filter-events";

function recordedEvents(): readonly ProjectedEvent[] {
  const parsed = parseRunTrace(loadTraceFixture());
  if (!parsed.ok) throw new Error("fixture must be valid");
  return projectReplay(parsed.trace, 99).flight.orderedEvents;
}

describe("filterLedgerEvents", () => {
  it("combines recorded lane, state, and evidence-field filters", () => {
    const events = filterLedgerEvents(recordedEvents(), {
      lane: "effect",
      query: "  REFUND_PAYMENT ",
      status: "compensating",
    });

    expect(events.length).toBeGreaterThan(0);
    expect(events.every(({ event }) => event.tool_name === "refund_payment")).toBe(true);
    expect(events.some(({ event }) => event.event_type === "compensation_outcome_recorded")).toBe(
      true,
    );
  });

  it("does not search redacted payloads or structured rationale", () => {
    const first = recordedEvents()[0];
    if (!first) throw new Error("fixture must contain an event");
    const hidden: ProjectedEvent = {
      ...first,
      event: {
        ...first.event,
        rationale: { concealed: "do-not-index-this-secret" },
        redacted_output: { concealed: "do-not-index-this-secret" },
      },
    };

    expect(
      filterLedgerEvents([hidden], {
        lane: "all",
        query: "do-not-index-this-secret",
        status: "all",
      }),
    ).toEqual([]);
  });

  it("searches a string receipt reference but ignores non-string receipt data", () => {
    const first = recordedEvents()[0];
    if (!first) throw new Error("fixture must contain an event");
    const referenced = {
      ...first,
      event: { ...first.event, receipt: { receipt_ref: "rcpt_safe" } },
    };
    const nonString = { ...first, event: { ...first.event, receipt: { receipt_ref: 7 } } };
    const filter = { lane: "all", query: "rcpt_safe", status: "all" } as const;

    expect(filterLedgerEvents([referenced, nonString], filter)).toEqual([referenced]);
  });

  it("searches known nested receipt references from real recorder evidence", () => {
    const referenced = recordedEvents().find(
      ({ event }) => typeof event.receipt?.receipt_ref === "string",
    );
    const reference = referenced?.event.receipt?.receipt_ref;
    if (typeof reference !== "string") throw new Error("fixture must contain a receipt reference");
    const matches = filterLedgerEvents(recordedEvents(), {
      lane: "all",
      query: reference,
      status: "all",
    });

    expect(matches.length).toBeGreaterThan(0);
    expect(matches).toContainEqual(referenced);
  });
});
