import type { LosslessNumber } from "lossless-json";
import { z } from "zod";
import {
  validLosslessNumber,
  validNonnegativeLosslessInteger,
  validPositiveLosslessInteger,
} from "./lossless-json";

const MAX_NAME = 200;
const MAX_TEXT = 500;
const MAX_STRING = 20_000;
const digestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const eventIdSchema = z.string().regex(/^evt_[a-z0-9]{16,64}$/);
const operationIdSchema = z.string().regex(/^op_[a-f0-9]{64}$/);
const stepIdSchema = z.string().regex(/^step_[a-z0-9]{8,64}$/);
const traceIdSchema = z.string().regex(/^trace_[a-z0-9]{16,64}$/);
const boundedNameSchema = z.string().min(1).max(MAX_NAME);
export const utcTimestampPattern =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(?:Z|\+00:00)$/;
const utcSchema = z.string().regex(utcTimestampPattern);
const positiveIntegerSchema = z.union([
  z.number().int().positive(),
  z.custom<LosslessNumber>(validPositiveLosslessInteger),
]);

export type JsonValue =
  | null
  | boolean
  | number
  | LosslessNumber
  | string
  | JsonValue[]
  | JsonObject;

export interface JsonObject {
  readonly [key: string]: JsonValue;
}

export const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.null(),
    z.boolean(),
    z.number().finite(),
    z.custom<LosslessNumber>(validLosslessNumber),
    z.string().max(MAX_STRING),
    z.array(jsonValueSchema),
    z.record(z.string().max(MAX_STRING), jsonValueSchema),
  ]),
);

const jsonObjectSchema = z.record(z.string().max(MAX_STRING), jsonValueSchema);

export const sagaStatusSchema = z.enum([
  "created",
  "running",
  "recovery_plan_required",
  "retry_wait",
  "reconciling_unknown",
  "compensating",
  "human_required",
  "succeeded_verified",
  "compensated_verified",
  "aborted_clean",
  "resolved_with_exception",
]);

export const traceEventSchema = z
  .object({
    event_id: eventIdSchema,
    saga_seq: z.number().int().positive(),
    recorded_at: utcSchema,
    authority: z.enum(["agent", "policy", "kernel", "effect", "compensation", "proof", "human"]),
    event_type: boundedNameSchema,
    actor: boundedNameSchema,
    trace_id: traceIdSchema,
    definition_version: boundedNameSchema,
    fence_token: positiveIntegerSchema.nullable(),
    before_status: sagaStatusSchema.nullable(),
    after_status: sagaStatusSchema,
    operation_id: operationIdSchema.nullable(),
    step_instance_id: stepIdSchema.nullable(),
    direction: z.enum(["forward", "compensation"]).nullable(),
    semantic_generation: z
      .union([
        z.number().int().nonnegative(),
        z.custom<LosslessNumber>(validNonnegativeLosslessInteger),
      ])
      .nullable(),
    attempt: positiveIntegerSchema.nullable(),
    tool_name: boundedNameSchema.nullable(),
    compensates_operation_id: operationIdSchema.nullable(),
    redacted_input: jsonObjectSchema.nullable(),
    redacted_output: jsonObjectSchema.nullable(),
    rationale: jsonObjectSchema,
    policy_decision: jsonObjectSchema.nullable(),
    receipt: jsonObjectSchema.nullable(),
    correlation: z.string().min(1).max(MAX_TEXT).nullable(),
    input_hash: digestSchema.nullable(),
    output_hash: digestSchema.nullable(),
  })
  .strict();

export const traceProofSchema = z
  .object({
    source_event_id: eventIdSchema,
    source_event_seq: z.number().int().positive(),
    invariant_version: boundedNameSchema,
    evaluated_at_seq: z.number().int().positive(),
    target_status: sagaStatusSchema,
    rule_id: boundedNameSchema,
    inputs: z.null(),
    result: z.enum(["valid", "invalid"]),
    explanation: z.literal("ledger_recorded_invariant_result"),
  })
  .strict();

export const runTraceSchema = z
  .object({
    schema_version: z.literal("1.0"),
    run_id: traceIdSchema,
    saga_id: z.string().regex(/^saga_[a-z0-9]{16,64}$/),
    definition_version: boundedNameSchema,
    started_at: utcSchema,
    finished_at: utcSchema.nullable(),
    outcome: sagaStatusSchema,
    events: z.array(traceEventSchema).min(1).max(10_000),
    proofs: z.array(traceProofSchema).max(10_000),
    final_projection_hash: digestSchema,
  })
  .strict();

export type RunTrace = z.infer<typeof runTraceSchema>;
export type SagaStatus = z.infer<typeof sagaStatusSchema>;
export type TraceEvent = z.infer<typeof traceEventSchema>;
export type TraceProof = z.infer<typeof traceProofSchema>;
