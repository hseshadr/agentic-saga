import { LosslessNumber, parse } from "lossless-json";

const INTEGER = /^-?(?:0|[1-9]\d*)$/;

export function parseTraceJson(text: string): unknown {
  rejectPrototypeKeys(JSON.parse(text));
  return parse(text, null, { onDuplicateKey: rejectDuplicateKey, parseNumber });
}

export function stringifyTraceJson(value: unknown, space?: number): string {
  return serializeJson(value, { canonical: false, gap: " ".repeat(space ?? 0) }, 0);
}

export function stringifyCanonicalJson(value: unknown): string {
  return serializeJson(value, { canonical: true, gap: "" }, 0);
}

export function validLosslessNumber(value: unknown): value is LosslessNumber {
  if (!isActualLosslessNumber(value)) return false;
  return INTEGER.test(value.value) || Number.isFinite(Number(value.value));
}

export function validPositiveLosslessInteger(value: unknown): value is LosslessNumber {
  return isActualLosslessNumber(value) && INTEGER.test(value.value) && BigInt(value.value) > 0n;
}

export function validNonnegativeLosslessInteger(value: unknown): value is LosslessNumber {
  return isActualLosslessNumber(value) && INTEGER.test(value.value) && BigInt(value.value) >= 0n;
}

export function isActualLosslessNumber(value: unknown): value is LosslessNumber {
  return value instanceof LosslessNumber;
}

function parseNumber(value: string): number | LosslessNumber {
  const parsed = Number(value);
  if (INTEGER.test(value) && Number.isSafeInteger(parsed)) return parsed;
  return new LosslessNumber(value);
}

function rejectDuplicateKey(): never {
  throw new SyntaxError("duplicate JSON key");
}

function rejectPrototypeKeys(value: unknown): void {
  const pending = [value];
  while (pending.length > 0) {
    const current = pending.pop();
    if (typeof current !== "object" || current === null) continue;
    if (Object.hasOwn(current, "__proto__")) throw new SyntaxError("unsupported JSON object key");
    pending.push(...Object.values(current));
  }
}

interface JsonFormat {
  readonly canonical: boolean;
  readonly gap: string;
}

function serializeJson(value: unknown, format: JsonFormat, depth: number): string {
  if (isActualLosslessNumber(value)) {
    return format.canonical ? pythonNumberLiteral(value) : value.value;
  }
  if (value === null || ["boolean", "number", "string"].includes(typeof value)) {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return serializeItems("[", "]", value, format, depth);
  if (typeof value === "object") return serializeObject(value, format, depth);
  throw new TypeError("value is not JSON serializable");
}

function serializeObject(value: object, format: JsonFormat, depth: number): string {
  const separator = format.gap ? ": " : ":";
  const entries = Object.entries(value);
  if (format.canonical) entries.sort(([left], [right]) => compareCodePoints(left, right));
  const items = entries.map(
    ([key, item]) => `${JSON.stringify(key)}${separator}${serializeJson(item, format, depth + 1)}`,
  );
  return serializeItems("{", "}", items, format, depth, true);
}

function compareCodePoints(left: string, right: string): number {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (value) => value.codePointAt(0) ?? 0);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (leftPoints[index] ?? 0) - (rightPoints[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

function serializeItems(
  open: string,
  close: string,
  values: readonly unknown[],
  format: JsonFormat,
  depth: number,
  encoded = false,
): string {
  const items = encoded
    ? (values as readonly string[])
    : values.map((item) => serializeJson(item, format, depth + 1));
  if (items.length === 0) return `${open}${close}`;
  if (!format.gap) return `${open}${items.join(",")}${close}`;
  const indent = format.gap.repeat(depth + 1);
  return `${open}\n${indent}${items.join(`,\n${indent}`)}\n${format.gap.repeat(depth)}${close}`;
}

function pythonNumberLiteral(value: unknown): string {
  if (!isActualLosslessNumber(value)) throw new TypeError("expected a lossless number");
  if (INTEGER.test(value.value)) return value.value;
  return pythonFloatLiteral(Number(value.value));
}

function pythonFloatLiteral(value: number): string {
  if (Object.is(value, -0)) return "-0.0";
  const literal = String(value);
  const exponent = decimalExponent(literal);
  if (exponent < -4 || exponent >= 16) return scientificLiteral(literal, exponent);
  return literal.includes(".") ? literal : `${literal}.0`;
}

function decimalExponent(value: string): number {
  const unsigned = value.replace(/^-/, "");
  const exponent = unsigned.indexOf("e");
  if (exponent >= 0) return Number(unsigned.slice(exponent + 1));
  const point = unsigned.indexOf(".");
  const decimal = point >= 0 ? point : unsigned.length;
  const first = unsigned.search(/[1-9]/);
  if (first < 0) return 0;
  return first < decimal ? decimal - first - 1 : decimal - first;
}

function scientificLiteral(value: string, exponent: number): string {
  const [coefficient] = value.toLowerCase().split("e");
  if (!coefficient) throw new TypeError("expected a finite number");
  const sign = coefficient.startsWith("-") ? "-" : "";
  const digits = coefficient
    .replace("-", "")
    .replace(".", "")
    .replace(/^0+|0+$/g, "");
  const mantissa = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
  const exponentSign = exponent >= 0 ? "+" : "-";
  return `${sign}${mantissa}e${exponentSign}${String(Math.abs(exponent)).padStart(2, "0")}`;
}
