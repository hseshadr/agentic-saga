import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";
import { parseTraceJson } from "../trace/lossless-json";
import { parseRunTrace } from "../trace/parse-run-trace";
import type { RunTrace } from "../trace/schema";
import { type ScenarioIndex, type ScenarioIndexEntry, scenarioIndexSchema } from "./schema";

const MAX_INDEX_BYTES = 512 * 1024;
const MAX_TRACE_BYTES = 8 * 1024 * 1024;
const decoder = new TextDecoder("utf-8", { fatal: true });

export interface LoadSuccess<Value> {
  readonly ok: true;
  readonly value: Value;
}

export interface LoadFailure {
  readonly ok: false;
  readonly message: string;
}

export type LoadResult<Value> = LoadSuccess<Value> | LoadFailure;
type Fetcher = typeof fetch;

class SafeLoadError extends Error {}

export function parseScenarioIndex(input: unknown): LoadResult<ScenarioIndex> {
  const parsed = scenarioIndexSchema.safeParse(input);
  if (parsed.success) return { ok: true, value: parsed.data };
  const duplicate = parsed.error.issues.some((issue) => issue.message.includes("unique"));
  return failure(duplicate ? "Trace index run IDs must be unique." : "Trace index is invalid.");
}

export class ScenarioRepository {
  readonly #baseUrl: URL;
  readonly #fetcher: Fetcher;

  constructor(baseUrl: URL, fetcher: Fetcher = globalThis.fetch) {
    this.#baseUrl = baseUrl;
    this.#fetcher = fetcher;
  }

  async loadIndex(signal?: AbortSignal): Promise<LoadResult<ScenarioIndex>> {
    try {
      const bytes = await this.#load(
        new URL("index.json", this.#baseUrl),
        MAX_INDEX_BYTES,
        "Trace index could not be loaded.",
        signal,
      );
      return parseScenarioIndex(parseJson(bytes));
    } catch (error: unknown) {
      if (error instanceof SafeLoadError) return failure(error.message);
      return failure("Trace index could not be loaded.");
    }
  }

  async loadTrace(entry: ScenarioIndexEntry, signal?: AbortSignal): Promise<LoadResult<RunTrace>> {
    try {
      const bytes = await this.#load(
        new URL(entry.trace_ref, this.#baseUrl),
        MAX_TRACE_BYTES,
        "RunTrace could not be loaded.",
        signal,
      );
      requireDigest(bytes, entry.trace_sha256);
      const parsed = parseRunTrace(parseJson(bytes));
      return parsed.ok ? { ok: true, value: parsed.trace } : parsed;
    } catch (error: unknown) {
      if (error instanceof SafeLoadError) return failure(error.message);
      return failure("RunTrace could not be loaded.");
    }
  }

  async loadBuildVersion(signal?: AbortSignal): Promise<LoadResult<string>> {
    try {
      const response = await this.#fetcher.call(globalThis, new URL("../", this.#baseUrl), {
        cache: "no-store",
        ...(signal ? { signal } : {}),
      });
      if (!response.ok || !response.headers.get("content-type")?.includes("text/html"))
        return failure("Recorder build could not be checked.");
      requireDeclaredLength(response, MAX_INDEX_BYTES);
      const bytes = await readBoundedBody(response, MAX_INDEX_BYTES);
      const document = new DOMParser().parseFromString(decoder.decode(bytes), "text/html");
      return { ok: true, value: documentBuildVersion(document) };
    } catch {
      return failure("Recorder build could not be checked.");
    }
  }

  async #load(
    url: URL,
    limit: number,
    loadFailure: string,
    signal?: AbortSignal,
  ): Promise<Uint8Array> {
    const init: RequestInit = { cache: "no-store", ...(signal ? { signal } : {}) };
    const response = await this.#fetcher.call(globalThis, url, init);
    if (!response.ok) throw new SafeLoadError(loadFailure);
    requireJsonResponse(response);
    requireDeclaredLength(response, limit);
    return readBoundedBody(response, limit);
  }
}

export function documentBuildVersion(document: Document): string {
  return JSON.stringify(
    [...document.querySelectorAll('script[type="module"][src], link[rel="stylesheet"][href]')]
      .map((element) => element.getAttribute("src") ?? element.getAttribute("href"))
      .sort(),
  );
}

function requireJsonResponse(response: Response): void {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("json")) {
    throw new SafeLoadError("Trace response must be JSON.");
  }
}

function requireDeclaredLength(response: Response, limit: number): void {
  const header = response.headers.get("content-length");
  if (header !== null && Number(header) > limit) {
    throw new SafeLoadError("Trace response exceeds recorder safety bounds.");
  }
}

async function readBoundedBody(response: Response, limit: number): Promise<Uint8Array> {
  if (!response.body) throw new SafeLoadError("Trace response has no body.");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const next = await reader.read();
    if (next.done) break;
    size += next.value.byteLength;
    if (size > limit) {
      await reader.cancel();
      throw new SafeLoadError("Trace response exceeds recorder safety bounds.");
    }
    chunks.push(next.value);
  }
  return joinChunks(chunks, size);
}

function joinChunks(chunks: Uint8Array[], size: number): Uint8Array {
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return joined;
}

function parseJson(bytes: Uint8Array): unknown {
  try {
    return parseTraceJson(decoder.decode(bytes));
  } catch {
    throw new SafeLoadError("Trace response contains invalid JSON.");
  }
}

function requireDigest(bytes: Uint8Array, expected: string): void {
  if (bytesToHex(sha256(bytes)) !== expected) {
    throw new SafeLoadError("RunTrace content does not match its index digest.");
  }
}

function failure(message: string): LoadFailure {
  return { message, ok: false };
}
