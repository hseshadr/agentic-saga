import { evaluateProof } from "../proof/evaluate-proof";
import type { RunTrace, TraceEvent } from "../trace/schema";
import type { ScenarioIndexEntry } from "./schema";

export interface ScenarioAssessment {
  readonly state: "passed" | "failed" | "pending" | "unavailable";
  readonly label: string;
  readonly detail: string;
}

const supportedIds = new Set([
  "happy-path",
  "business-failure",
  "lost-response",
  "compensation-failure",
]);

export function supportsScenario(entry: ScenarioIndexEntry): boolean {
  return entry.presentation === "ecommerce" && supportedIds.has(entry.id);
}

export function assessScenario(entry: ScenarioIndexEntry, trace: RunTrace): ScenarioAssessment {
  const proof = evaluateProof(trace.events, trace.proofs, trace.outcome, true);
  if (!supportsScenario(entry)) {
    return {
      state: "unavailable",
      label: proof.terminalVerified ? "Verified" : "No assessment",
      detail: "No expected scenario result is defined for this recording.",
    };
  }
  if (trace.outcome === "running" || trace.outcome === "compensating") {
    return {
      state: "pending",
      label: "Pending",
      detail: "The recorded scenario has not finished.",
    };
  }
  if (entry.id === "compensation-failure" && trace.outcome === "human_required") {
    return assessSafeStop(trace);
  }
  const expected =
    entry.id === "happy-path" || entry.id === "lost-response"
      ? "succeeded_verified"
      : "compensated_verified";
  if (trace.outcome !== expected) {
    return failed(`Expected ${expected}; the recording ends as ${trace.outcome}.`);
  }
  if (!proof.terminalVerified)
    return failed("The expected outcome has no complete terminal proof.");
  if (entry.id === "lost-response" && !hasReconciledPayment(trace.events)) {
    return failed("No provider evidence confirms the payment after its reply was lost.");
  }
  if (entry.id === "compensation-failure" && !hasResolvedRefund(trace.events)) {
    return failed(
      "The uncertain refund has no matching human resolution before recovery completed.",
    );
  }
  return {
    state: "passed",
    label: "Passed",
    detail: passedDetail(entry.id),
  };
}

function passedDetail(id: string): string {
  if (id === "lost-response") return "Payment reconciled; order completed and verified.";
  if (id === "compensation-failure") return "Human review resolved the refund; recovery verified.";
  if (id === "business-failure") return "All earlier changes undone and verified.";
  return "Order completed and verified.";
}

function failed(detail: string): ScenarioAssessment {
  return { state: "failed", label: "Not passed", detail };
}

function hasReconciledPayment(events: readonly TraceEvent[]): boolean {
  const unknownPayments = new Set<string>();
  for (const event of events) {
    if (event.tool_name !== "charge_payment" || event.direction !== "forward") continue;
    if (
      event.operation_id !== null &&
      event.event_type === "effect_outcome_recorded" &&
      event.redacted_output?.kind === "outcome_unknown"
    )
      unknownPayments.add(event.operation_id);
    if (
      event.event_type === "reconciliation_recorded" &&
      event.redacted_output?.kind === "reconcile_effect_confirmed" &&
      event.receipt !== null &&
      event.operation_id !== null &&
      unknownPayments.has(event.operation_id)
    )
      return true;
  }
  return false;
}

function uncertainRefund(events: readonly TraceEvent[]): TraceEvent | undefined {
  const confirmedCharges = new Set<string>();
  for (const event of events) {
    if (
      event.tool_name === "charge_payment" &&
      event.direction === "forward" &&
      event.operation_id !== null &&
      event.redacted_output?.kind === "effect_confirmed" &&
      event.receipt !== null
    )
      confirmedCharges.add(event.operation_id);
    if (
      event.event_type === "compensation_outcome_recorded" &&
      event.direction === "compensation" &&
      event.tool_name === "refund_payment" &&
      event.operation_id !== null &&
      event.redacted_output?.kind === "outcome_unknown" &&
      event.compensates_operation_id !== null &&
      confirmedCharges.has(event.compensates_operation_id)
    )
      return event;
  }
  return undefined;
}

function refundPause(events: readonly TraceEvent[], refund: TraceEvent): TraceEvent | undefined {
  return events.find(
    (event) =>
      event.saga_seq > refund.saga_seq &&
      event.event_type === "human_required" &&
      event.authority === "workflow" &&
      event.before_status === "compensating" &&
      event.after_status === "human_required",
  );
}

function assessSafeStop(trace: RunTrace): ScenarioAssessment {
  const refund = uncertainRefund(trace.events);
  const pause = refund && refundPause(trace.events, refund);
  const stopped =
    refund &&
    pause &&
    trace.finished_at === null &&
    trace.events.at(-1)?.event_id === pause.event_id &&
    !trace.events.some((event) => event.saga_seq > refund.saga_seq && event.direction !== null);
  if (!stopped) return failed("No safe pause is recorded immediately after the uncertain refund.");
  return {
    state: "passed",
    label: "Safety check passed",
    detail: "Safe stop demonstrated — human review remains required.",
  };
}

function hasResolvedRefund(events: readonly TraceEvent[]): boolean {
  const refund = uncertainRefund(events);
  const pause = refund && refundPause(events, refund);
  if (!refund || !pause) return false;
  const resolution = events.find(
    (event) =>
      event.saga_seq > pause.saga_seq &&
      event.event_type === "human_resolved" &&
      event.authority === "human" &&
      event.operation_id === refund.operation_id &&
      event.before_status === "human_required" &&
      event.after_status === "compensating",
  );
  const terminal = events.findLast((event) => event.event_type === "terminal_assigned");
  if (!resolution || !terminal || resolution.saga_seq >= terminal.saga_seq) return false;
  return !events.some(
    (event) =>
      event.saga_seq > refund.saga_seq &&
      event.saga_seq < resolution.saga_seq &&
      event.direction !== null,
  );
}
