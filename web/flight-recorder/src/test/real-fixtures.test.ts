import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parseScenarioIndex } from "../scenarios/repository";
import { parseRunTrace } from "../trace/parse-run-trace";
import { loadIndexFixture } from "./load-index-fixture";

const ROOT = resolve(process.cwd(), "../../examples/ecommerce/flight-recorder/traces");

describe("real flight-recorder fixtures", () => {
  it("indexes four honest kernel-generated paths with verified digests", () => {
    const parsed = parseScenarioIndex(loadIndexFixture());
    if (!parsed.ok) throw new Error("fixture index must be valid");
    expect(parsed.value.runs.map(({ id }) => id)).toEqual([
      "happy-path",
      "lost-response",
      "business-failure",
      "compensation-failure",
    ]);
    for (const entry of parsed.value.runs) {
      const body = readFileSync(resolve(ROOT, entry.trace_ref));
      expect(createHash("sha256").update(body).digest("hex")).toBe(entry.trace_sha256);
      expect(parseRunTrace(JSON.parse(body.toString("utf8"))).ok).toBe(true);
    }
  });

  it("records success and lost-response reconciliation without synthetic events", () => {
    const happy = readTrace("happy-path");
    expect(happy.outcome).toBe("succeeded_verified");
    expect(happy.events.some((event) => event.authority === "compensation")).toBe(false);
    const lost = readTrace("lost-response");
    expect(lost.outcome).toBe("succeeded_verified");
    expect(lost.events.some((event) => event.event_type === "reconciliation_recorded")).toBe(true);
    expect(lost.events.some((event) => event.event_type === "agent_proposal_accepted")).toBe(false);
  });
});

function readTrace(name: string) {
  const parsed = parseRunTrace(JSON.parse(readFileSync(resolve(ROOT, `${name}.json`), "utf8")));
  if (!parsed.ok) throw new Error("real fixture must satisfy RunTrace");
  return parsed.trace;
}
