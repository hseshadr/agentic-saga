import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const fixturePath = resolve(
  process.cwd(),
  "../../examples/ecommerce/flight-recorder/traces/index.json",
);

export function loadIndexFixture(): unknown {
  return JSON.parse(readFileSync(fixturePath, "utf8"));
}
