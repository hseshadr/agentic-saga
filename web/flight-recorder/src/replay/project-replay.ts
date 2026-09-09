import { evaluateProof, type ProofEvaluation } from "../proof/evaluate-proof";
import { type FlightProjection, projectFlight } from "../trace/flight-projection";
import type { RunTrace, SagaStatus, TraceEvent, TraceProof } from "../trace/schema";

export interface ReplayProjection {
  readonly currentEvent: TraceEvent;
  readonly currentStatus: SagaStatus;
  readonly cursor: number;
  readonly events: readonly TraceEvent[];
  readonly flight: FlightProjection;
  readonly humanRequired: boolean;
  readonly proof: ProofEvaluation;
  readonly proofs: readonly TraceProof[];
  readonly terminalVerified: boolean;
}

export function projectReplay(trace: RunTrace, requestedCursor: number): ReplayProjection {
  const cursor = Math.max(0, Math.min(Math.trunc(requestedCursor), trace.events.length - 1));
  const events = trace.events.slice(0, cursor + 1);
  const currentEvent = events.at(-1);
  if (!currentEvent) throw new TypeError("validated RunTrace must contain ledger evidence");
  const visibleProofs = trace.proofs.filter(
    (proof) => proof.source_event_seq <= currentEvent.saga_seq,
  );
  const currentStatus = currentEvent.after_status;
  const atEnd = cursor === trace.events.length - 1;
  const proof = evaluateProof(events, visibleProofs, currentStatus, atEnd);
  const proofs = proof.records;
  const flight = projectFlight({ ...trace, events, proofs: [...proofs] });
  return {
    currentEvent,
    currentStatus,
    cursor,
    events,
    flight,
    humanRequired: currentStatus === "human_required",
    proof,
    proofs,
    terminalVerified: proof.terminalVerified,
  };
}
