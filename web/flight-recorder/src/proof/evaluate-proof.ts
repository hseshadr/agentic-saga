import type { SagaStatus, TraceEvent, TraceProof } from "../trace/schema";

export interface ProofEvaluation {
  readonly expectedRuleIds: readonly string[];
  readonly humanRequired: boolean;
  readonly invalidRuleIds: readonly string[];
  readonly missingRuleIds: readonly string[];
  readonly records: readonly TraceProof[];
  readonly sourceEvent: TraceEvent | undefined;
  readonly terminalVerified: boolean;
  readonly validRuleIds: readonly string[];
}

const terminalStates = new Set<SagaStatus>([
  "aborted_clean",
  "compensated_verified",
  "resolved_with_exception",
  "succeeded_verified",
]);

export function evaluateProof(
  events: readonly TraceEvent[],
  proofs: readonly TraceProof[],
  status: SagaStatus,
  atEnd: boolean,
): ProofEvaluation {
  const sourceEvent = events.findLast((event) => event.event_type === "invariant_evaluated");
  const expectation = sourceEvent ? expectationFrom(sourceEvent) : undefined;
  const expectedRuleIds = expectation?.ruleIds ?? [];
  const group = proofs.filter((proof) => sameSource(proof, sourceEvent, expectation));
  const records = uniqueRuleProofs(group);
  const result = (ruleId: string) => exactRuleProof(group, ruleId);
  const validRuleIds = expectedRuleIds.filter((ruleId) => result(ruleId)?.result === "valid");
  const invalidRuleIds = expectedRuleIds.filter((ruleId) => result(ruleId)?.result === "invalid");
  const missingRuleIds = expectedRuleIds.filter((ruleId) => result(ruleId) === undefined);
  const complete =
    validRuleIds.length === expectedRuleIds.length && group.length === expectedRuleIds.length;
  const terminalVerified = Boolean(
    atEnd &&
      terminalStates.has(status) &&
      expectation?.allPassed &&
      expectation.targetStatus === status &&
      expectedRuleIds.length &&
      complete,
  );
  return {
    expectedRuleIds,
    humanRequired: status === "human_required",
    invalidRuleIds,
    missingRuleIds,
    records,
    sourceEvent,
    terminalVerified,
    validRuleIds,
  };
}

interface Expectation {
  readonly allPassed: boolean;
  readonly invariantVersion: string;
  readonly ruleIds: readonly string[];
  readonly targetStatus: string;
}

function expectationFrom(event: TraceEvent): Expectation | undefined {
  const { all_passed, invariant_version, results, target_status } = event.rationale;
  if (
    typeof all_passed !== "boolean" ||
    typeof invariant_version !== "string" ||
    typeof target_status !== "string" ||
    !isBooleanRecord(results)
  )
    return undefined;
  return {
    allPassed: all_passed && Object.values(results).every(Boolean),
    invariantVersion: invariant_version,
    ruleIds: Object.keys(results),
    targetStatus: target_status,
  };
}

function isBooleanRecord(value: unknown): value is Readonly<Record<string, boolean>> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((entry) => typeof entry === "boolean")
  );
}

function sameSource(
  proof: TraceProof,
  source: TraceEvent | undefined,
  expectation: Expectation | undefined,
): boolean {
  return Boolean(
    source &&
      expectation &&
      proof.source_event_id === source.event_id &&
      proof.source_event_seq === source.saga_seq &&
      proof.evaluated_at_seq === source.saga_seq - 1 &&
      proof.invariant_version === expectation.invariantVersion &&
      proof.target_status === expectation.targetStatus,
  );
}

function exactRuleProof(proofs: readonly TraceProof[], ruleId: string): TraceProof | undefined {
  const matches = proofs.filter((proof) => proof.rule_id === ruleId);
  return matches.length === 1 ? matches[0] : undefined;
}

function uniqueRuleProofs(proofs: readonly TraceProof[]): readonly TraceProof[] {
  const seen = new Set<string>();
  return proofs.filter((proof) => {
    if (seen.has(proof.rule_id)) return false;
    seen.add(proof.rule_id);
    return true;
  });
}
