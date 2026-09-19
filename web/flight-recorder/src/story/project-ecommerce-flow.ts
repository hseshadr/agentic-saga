import type { ReplayProjection } from "../replay/project-replay";
import type { TraceEvent } from "../trace/schema";

export type FlowPhase = "compensated" | "compensating" | "human" | "running" | "succeeded";
export type FlowStepState =
  | "active"
  | "attention"
  | "checking"
  | "complete"
  | "failed"
  | "reversed"
  | "waiting";

export interface FlowStep {
  readonly detail: string;
  readonly id: string;
  readonly label: string;
  readonly state: FlowStepState;
}

export interface EcommerceFlow {
  readonly detail: string;
  readonly forward: readonly FlowStep[];
  readonly headline: string;
  readonly phase: FlowPhase;
  readonly rollback: readonly FlowStep[];
}

interface StepDefinition {
  readonly detail: string;
  readonly id: string;
  readonly label: string;
  readonly tool: string;
}

const forwardDefinitions: readonly StepDefinition[] = [
  {
    id: "reserve-inventory",
    label: "Reserve item",
    detail: "Reserve the requested stock",
    tool: "reserve_inventory",
  },
  {
    id: "charge-payment",
    label: "Charge payment",
    detail: "Capture the order total",
    tool: "charge_payment",
  },
  {
    id: "schedule-fulfillment",
    label: "Arrange delivery",
    detail: "Ask fulfillment to ship",
    tool: "schedule_fulfillment",
  },
  {
    id: "verify-order",
    label: "Verify order",
    detail: "Read the final provider state",
    tool: "verify_order",
  },
];

const rollbackDefinitions: readonly StepDefinition[] = [
  {
    id: "cancel-fulfillment",
    label: "Cancel delivery",
    detail: "Undo fulfillment first",
    tool: "cancel_fulfillment",
  },
  {
    id: "refund-payment",
    label: "Refund payment",
    detail: "Return the captured total",
    tool: "refund_payment",
  },
  {
    id: "release-inventory",
    label: "Release stock",
    detail: "Return the held item",
    tool: "release_inventory",
  },
];

const reversalTools: Readonly<Record<string, string>> = {
  "charge-payment": "refund_payment",
  "reserve-inventory": "release_inventory",
  "schedule-fulfillment": "cancel_fulfillment",
};

export function projectEcommerceFlow(projection: ReplayProjection): EcommerceFlow {
  const phase = phaseFor(projection);
  return {
    detail: detailFor(phase),
    forward: forwardDefinitions.map((definition) => forwardStep(definition, projection.events)),
    headline: headlineFor(projection),
    phase,
    rollback: rollbackDefinitions.map((definition) => stepFor(definition, projection.events)),
  };
}

function forwardStep(definition: StepDefinition, events: readonly TraceEvent[]): FlowStep {
  const reversal = reversalTools[definition.id];
  if (reversal && toolState(events, reversal) === "complete") return step(definition, "reversed");
  if (definition.tool === "verify_order") return step(definition, verificationState(events));
  if (definition.tool === "schedule_fulfillment" && fulfillmentRejected(events)) {
    return step(definition, "failed");
  }
  return stepFor(definition, events);
}

function stepFor(definition: StepDefinition, events: readonly TraceEvent[]): FlowStep {
  return step(definition, toolState(events, definition.tool));
}

function step(definition: StepDefinition, state: FlowStepState): FlowStep {
  return { detail: definition.detail, id: definition.id, label: definition.label, state };
}

function toolState(events: readonly TraceEvent[], tool: string): FlowStepState {
  const matching = events.filter((event) => event.tool_name === tool);
  if (matching.length === 0) return "waiting";
  if (matching.some(confirmsEffect)) return "complete";
  if (matching.some(needsAttention)) {
    const needsHuman = events.at(-1)?.after_status === "human_required";
    return needsHuman && matching.some((event) => event.direction === "compensation")
      ? "attention"
      : "checking";
  }
  return matching.some((event) => event.event_type.endsWith("observed")) ? "complete" : "active";
}

function confirmsEffect(event: TraceEvent): boolean {
  return (
    outputKind(event) === "effect_confirmed" || outputKind(event) === "reconcile_effect_confirmed"
  );
}

function needsAttention(event: TraceEvent): boolean {
  const kind = outputKind(event);
  return (
    kind === "outcome_unknown" || kind === "reconcile_conflict" || kind === "reconcile_pending"
  );
}

function outputKind(event: TraceEvent): unknown {
  return event.redacted_output?.kind;
}

function verificationState(events: readonly TraceEvent[]): FlowStepState {
  const observed = events.findLast(
    (event) => event.tool_name === "verify_order" && event.event_type === "invariant_evaluated",
  );
  if (observed) return observed.redacted_output?.verified === true ? "complete" : "failed";
  return events.some((event) => event.tool_name === "verify_order") ? "active" : "waiting";
}

function fulfillmentRejected(events: readonly TraceEvent[]): boolean {
  return events.some(
    (event) => event.tool_name === "verify_order" && event.redacted_output?.verified === false,
  );
}

function phaseFor(projection: ReplayProjection): FlowPhase {
  if (projection.currentStatus === "human_required") return "human";
  if (projection.currentStatus === "compensated_verified") return "compensated";
  if (projection.currentStatus === "succeeded_verified") return "succeeded";
  if (projection.currentStatus === "compensating") return "compensating";
  return "running";
}

function headlineFor(projection: ReplayProjection): string {
  if (projection.currentStatus === "human_required") return "Paused safely for human review";
  if (projection.currentStatus === "compensated_verified") {
    return "Every completed change was safely undone";
  }
  if (projection.currentStatus === "succeeded_verified") return "Order completed safely";
  if (outputKind(projection.currentEvent) === "outcome_unknown") {
    return "Checking what the payment provider actually did";
  }
  if (projection.currentEvent.event_type === "reconciliation_recorded") {
    return "Provider evidence confirms the payment completed";
  }
  if (projection.currentEvent.event_type === "compensation_started") {
    return "A later step failed—starting safe undo actions";
  }
  const label = labelForTool(projection.currentEvent.tool_name);
  if (label) return `${actionVerb(projection.currentEvent)} ${label.toLowerCase()}`;
  return "The agent is reading the order goal";
}

function actionVerb(event: TraceEvent): string {
  if (event.event_type.endsWith("observed") || event.event_type === "effect_outcome_recorded") {
    return "Completed";
  }
  return event.direction === "compensation" ? "Undoing" : "Working on";
}

function labelForTool(tool: string | null): string | undefined {
  return [...forwardDefinitions, ...rollbackDefinitions].find((item) => item.tool === tool)?.label;
}

function detailFor(phase: FlowPhase): string {
  if (phase === "human")
    return "The recovery result is still uncertain, so the system stops before making another change.";
  if (phase === "compensated")
    return "The order could not finish, but inventory, payment, and fulfillment are verified safe.";
  if (phase === "compensating")
    return "The safety layer reveals one valid undo at a time, in reverse order.";
  if (phase === "succeeded")
    return "Fresh evidence confirms the inventory, payment, and delivery state.";
  return "The agent chooses what to do next; deterministic safety code approves and records every change.";
}
