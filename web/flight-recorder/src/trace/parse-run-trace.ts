import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { isActualLosslessNumber, stringifyCanonicalJson } from "./lossless-json";
import {
  type JsonObject,
  type JsonValue,
  type RunTrace,
  runTraceSchema,
  utcTimestampPattern,
} from "./schema";

const MAX_DEPTH = 16;
const MAX_NODES = 100_000;
const MAX_STRING = 20_000;
const MAX_TRACE_BYTES = 8 * 1024 * 1024;
const encoder = new TextEncoder();
const UTC_PARTS = new RegExp(utcTimestampPattern.source);
const terminalStates = new Set(["succeeded_verified", "compensated_verified", "aborted_clean"]);

export interface ParseSuccess {
  readonly ok: true;
  readonly trace: RunTrace;
}

export interface ParseFailure {
  readonly ok: false;
  readonly message: string;
}

export type ParseResult = ParseSuccess | ParseFailure;

interface PendingValue {
  readonly depth: number;
  readonly value: unknown;
}

class TraceValidationError extends Error {}

export function parseRunTrace(input: unknown): ParseResult {
  try {
    requireSafetyBounds(input);
    requireSupportedVersion(input);
    const parsed = runTraceSchema.safeParse(input);
    if (!parsed.success) return failure("RunTrace does not match the strict 1.0 contract.");
    validateRelations(parsed.data);
    return { ok: true, trace: parsed.data };
  } catch (error: unknown) {
    if (error instanceof TraceValidationError) return failure(error.message);
    return failure("RunTrace could not be validated safely.");
  }
}

function requireSupportedVersion(input: unknown): void {
  if (!isObject(input) || input.schema_version !== "1.0") {
    throw new TraceValidationError("This recorder supports RunTrace 1.0 only.");
  }
}

function requireSafetyBounds(input: unknown): void {
  const pending: PendingValue[] = [{ depth: 0, value: input }];
  const seen = new WeakSet<object>();
  let nodes = 0;
  let bytes = 0;
  while (pending.length > 0) {
    const current = pending.pop();
    if (!current) break;
    nodes += 1;
    bytes += inspectValue(current, pending, seen);
    if (current.depth > MAX_DEPTH || nodes > MAX_NODES || bytes > MAX_TRACE_BYTES) {
      throw new TraceValidationError("RunTrace exceeds recorder safety bounds.");
    }
  }
}

function inspectValue(
  current: PendingValue,
  pending: PendingValue[],
  seen: WeakSet<object>,
): number {
  if (typeof current.value === "string") return boundedStringBytes(current.value);
  if (isActualLosslessNumber(current.value)) return boundedStringBytes(current.value.value);
  if (typeof current.value !== "object" || current.value === null) return 8;
  if (seen.has(current.value)) throw new TraceValidationError("RunTrace contains a cycle.");
  seen.add(current.value);
  const entries = Array.isArray(current.value)
    ? current.value.map((value) => ["", value] as const)
    : Object.entries(current.value);
  for (const [, value] of entries) pending.push({ depth: current.depth + 1, value });
  return entries.reduce((total, [key]) => total + boundedStringBytes(key) + 2, 2);
}

function boundedStringBytes(value: string): number {
  if (value.length > MAX_STRING) {
    throw new TraceValidationError("RunTrace exceeds recorder safety bounds.");
  }
  return encoder.encode(value).byteLength;
}

function validateRelations(trace: RunTrace): void {
  requireEventSequence(trace);
  requireTraceIdentity(trace);
  requireTraceTimes(trace);
  requireStatusChain(trace);
  requireProofSources(trace);
  requireEvidenceHashes(trace);
}

function requireEventSequence(trace: RunTrace): void {
  if (trace.events.some((event, index) => event.saga_seq !== index + 1)) {
    throw new TraceValidationError("RunTrace events must be contiguous and ordered.");
  }
}

function requireTraceIdentity(trace: RunTrace): void {
  const first = trace.events[0];
  const last = trace.events.at(-1);
  if (!first || !last) throw new TraceValidationError("RunTrace requires ledger evidence.");
  if (trace.run_id !== first.trace_id || !sameInstant(trace.started_at, first.recorded_at)) {
    throw new TraceValidationError("RunTrace header does not match its first event.");
  }
  if (trace.events.some((event) => event.definition_version !== trace.definition_version)) {
    throw new TraceValidationError("RunTrace definition changed during the run.");
  }
  if (trace.outcome !== last.after_status) {
    throw new TraceValidationError("RunTrace outcome does not match its final event.");
  }
}

function requireTraceTimes(trace: RunTrace): void {
  const times = trace.events.map((event) => utcNanoseconds(event.recorded_at));
  if (times.some((time, index) => index > 0 && time < (times[index - 1] ?? 0n))) {
    throw new TraceValidationError("RunTrace event times must be monotonic UTC timestamps.");
  }
  const expected = terminalStates.has(trace.outcome) ? trace.events.at(-1)?.recorded_at : null;
  if (!sameOptionalInstant(trace.finished_at, expected)) {
    throw new TraceValidationError("RunTrace finish time does not match terminal evidence.");
  }
}

function sameOptionalInstant(left: string | null, right: string | null | undefined): boolean {
  if (left === null || right == null) return left === null && right == null;
  return sameInstant(left, right);
}

function sameInstant(left: string, right: string): boolean {
  return utcNanoseconds(left) === utcNanoseconds(right);
}

function utcNanoseconds(value: string): bigint {
  const parts = UTC_PARTS.exec(value);
  if (!parts) throw new TraceValidationError("RunTrace contains an invalid UTC timestamp.");
  const [, year = "", month = "", day = "", hour = "", minute = "", second = "", fraction = ""] =
    parts;
  const date = new Date(0);
  date.setUTCFullYear(Number(year), Number(month) - 1, Number(day));
  date.setUTCHours(Number(hour), Number(minute), Number(second), 0);
  requireCalendarParts(date, [year, month, day, hour, minute, second]);
  return BigInt(date.getTime()) * 1_000_000n + BigInt(fraction.padEnd(9, "0"));
}

function requireCalendarParts(date: Date, parts: readonly string[]): void {
  const actual = [
    date.getUTCFullYear(),
    date.getUTCMonth() + 1,
    date.getUTCDate(),
    date.getUTCHours(),
    date.getUTCMinutes(),
    date.getUTCSeconds(),
  ];
  if (Number(parts[0]) < 1 || actual.some((value, index) => value !== Number(parts[index]))) {
    throw new TraceValidationError("RunTrace contains an invalid UTC timestamp.");
  }
}

function requireStatusChain(trace: RunTrace): void {
  const first = trace.events[0];
  if (first?.before_status !== null) {
    throw new TraceValidationError("RunTrace statuses must form one causal chain.");
  }
  for (let index = 1; index < trace.events.length; index += 1) {
    if (trace.events[index - 1]?.after_status !== trace.events[index]?.before_status) {
      throw new TraceValidationError("RunTrace statuses must form one causal chain.");
    }
  }
}

function requireProofSources(trace: RunTrace): void {
  for (const proof of trace.proofs) {
    const source = trace.events[proof.source_event_seq - 1];
    const validSource = source?.event_id === proof.source_event_id;
    const validKind = source?.event_type === "invariant_evaluated";
    if (!validSource || !validKind || proof.evaluated_at_seq !== proof.source_event_seq - 1) {
      throw new TraceValidationError("RunTrace proof source is absent or not invariant evidence.");
    }
  }
}

function requireEvidenceHashes(trace: RunTrace): void {
  for (const event of trace.events) {
    requireHash(event.redacted_input, event.input_hash);
    requireHash(event.redacted_output, event.output_hash);
  }
}

function requireHash(value: JsonObject | null, digest: string | null): void {
  if (value === null && digest === null) return;
  if (value === null || digest !== hashJson(value)) {
    throw new TraceValidationError("RunTrace evidence hash does not match its redacted value.");
  }
}

function hashJson(value: JsonValue): string {
  const canonical = stringifyCanonicalJson(value);
  return bytesToHex(sha256(encoder.encode(canonical)));
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function failure(message: string): ParseFailure {
  return { message, ok: false };
}
