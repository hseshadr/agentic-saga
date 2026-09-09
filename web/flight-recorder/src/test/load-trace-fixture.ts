import { readFileSync } from "node:fs";
import { resolve } from "node:path";

export type TraceFixtureName =
  | "business-failure"
  | "compensation-failure"
  | "happy-path"
  | "lost-response";

export function loadTraceFixture(name: TraceFixtureName = "business-failure"): unknown {
  const path = resolve(
    process.cwd(),
    `../../examples/ecommerce/flight-recorder/traces/${name}.json`,
  );
  return JSON.parse(readFileSync(path, "utf8"));
}
