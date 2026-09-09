import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const tokenCss = readFileSync(resolve(process.cwd(), "src/styles/tokens.css"), "utf8");
const componentCss = readFileSync(
  resolve(process.cwd(), "src/components/recorder-workbench.module.css"),
  "utf8",
);

function color(name: string): string {
  const match = tokenCss.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`));
  if (!match?.[1]) throw new Error(`missing color token: ${name}`);
  return match[1];
}

function luminance(value: string): number {
  const channels = value
    .slice(1)
    .match(/.{2}/g)
    ?.map((item) => Number.parseInt(item, 16) / 255);
  if (!channels) throw new Error("invalid color token");
  const linear = channels.map((item) =>
    item <= 0.04045 ? item / 12.92 : ((item + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * (linear[0] ?? 0) + 0.7152 * (linear[1] ?? 0) + 0.0722 * (linear[2] ?? 0);
}

function contrast(left: string, right: string): number {
  const values = [luminance(left), luminance(right)].sort((a, b) => b - a);
  return ((values[0] ?? 0) + 0.05) / ((values[1] ?? 0) + 0.05);
}

describe("visual tokens", () => {
  it.each([
    ["deck-blue", "paper"],
    ["muted-ink", "paper"],
    ["deck-blue", "paper-raised"],
    ["deck-blue", "signal-muted"],
    ["proof", "paper-raised"],
    ["stop", "paper-raised"],
  ])("keeps %s text WCAG AA against %s", (foreground, background) => {
    expect(contrast(color(foreground), color(background))).toBeGreaterThanOrEqual(4.5);
  });

  it("uses no gradients or network assets", () => {
    expect(`${tokenCss}${componentCss}`).not.toMatch(/gradient|url\s*\(/i);
  });

  it("keeps non-causal proof text comfortably above AA", () => {
    expect(contrast(color("proof"), color("signal-muted"))).toBeGreaterThanOrEqual(4.75);
  });

  it("does not dim causal controls below their declared colors", () => {
    expect(componentCss).not.toMatch(/opacity\s*:/i);
  });

  it("stacks the 1180-pixel workspace before its columns can collide", () => {
    const responsive = componentCss.match(/@media \(max-width: 1180px\)([\s\S]*?)@media/);
    expect(responsive?.[1]).toMatch(/\.workspace[\s\S]*grid-template-columns:\s*1fr/);
  });
});
