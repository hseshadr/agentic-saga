import type { SagaStatus, TraceEvent } from "../trace/schema";

/**
 * Recovery evidence for a compensated run.
 *
 * The Workflow records `compensation_completed` after its journaled compensations ran
 * newest-first. That record is not an invariant proof, so it is never treated as one:
 * each operation it lists must also carry its own recorded compensation outcome — a
 * confirmed provider receipt, or an unknown outcome followed by a matching human
 * resolution — before the recovery counts as complete.
 */
export interface RecoveryEvaluation {
  readonly compensatedOperationIds: readonly string[];
  readonly completed: boolean;
  readonly confirmedOperationIds: readonly string[];
  readonly record: TraceEvent | undefined;
}

const confirmedKinds = new Set(["effect_confirmed", "reconcile_effect_confirmed"]);

export function evaluateRecovery(
  events: readonly TraceEvent[],
  status: SagaStatus,
  atEnd: boolean,
): RecoveryEvaluation {
  const record = events.findLast((event) => event.event_type === "compensation_completed");
  const compensatedOperationIds = record ? operationIds(record) : [];
  const confirmedOperationIds = compensatedOperationIds.filter((id) => confirmed(events, id));
  const completed = Boolean(
    atEnd &&
      record &&
      status === "compensated_verified" &&
      record.rationale.target_status === status &&
      compensatedOperationIds.length &&
      confirmedOperationIds.length === compensatedOperationIds.length,
  );
  return { compensatedOperationIds, completed, confirmedOperationIds, record };
}

function operationIds(record: TraceEvent): readonly string[] {
  const ids = record.rationale.compensated_operation_ids;
  if (!Array.isArray(ids)) return [];
  return ids.filter((id): id is string => typeof id === "string");
}

function confirmed(events: readonly TraceEvent[], forwardId: string): boolean {
  return events.some(
    (event) =>
      event.direction === "compensation" &&
      event.compensates_operation_id === forwardId &&
      (confirmedOutcome(event) || humanResolved(events, event)),
  );
}

function confirmedOutcome(event: TraceEvent): boolean {
  const kind = event.redacted_output?.kind;
  return typeof kind === "string" && confirmedKinds.has(kind);
}

function humanResolved(events: readonly TraceEvent[], compensation: TraceEvent): boolean {
  return events.some(
    (event) =>
      event.saga_seq > compensation.saga_seq &&
      event.event_type === "human_resolved" &&
      event.authority === "human" &&
      event.operation_id !== null &&
      event.operation_id === compensation.operation_id,
  );
}

/**
 * A compensated terminal is backed by recorded recovery evidence; every other terminal by
 * its recorded invariant proof. A proof claiming compensated_verified is never enough.
 */
export function terminalVerified(
  status: SagaStatus,
  proofVerified: boolean,
  recovery: RecoveryEvaluation,
): boolean {
  return status === "compensated_verified" ? recovery.completed : proofVerified;
}
