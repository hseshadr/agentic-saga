import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { createInterface } from "node:readline";
import { expect, type Page, test } from "@playwright/test";

const runs = [
  ["happy-path", "Succeeded verified", "Terminal assigned"],
  ["lost-response", "Succeeded verified", "Reconciliation recorded"],
  ["business-failure", "Compensated verified", "Compensation started"],
  ["compensation-failure", "Human required", "Human required"],
] as const;
const STOP_SIGNALS = ["SIGINT", "SIGTERM", "SIGKILL"] as const;
const STOP_GRACE_MS = 2_000;

interface NetworkEvidence {
  readonly externalOrigins: string[];
  readonly tracePaths: string[];
  readonly webSockets: string[];
}

function packagedCli(): string {
  const executable = process.env.AGENTIC_SAGA_CLI;
  if (!executable || !path.isAbsolute(executable)) {
    throw new Error("AGENTIC_SAGA_CLI must name the absolute wheel-installed executable");
  }
  return executable;
}

function launchRecorder(scenario: string): ChildProcessWithoutNullStreams {
  const environment = { ...process.env };
  delete environment.PYTHONPATH;
  delete environment.OPENROUTER_API_KEY;
  return spawn(packagedCli(), ["demo", "--scenario", scenario, "--port", "0"], {
    env: environment,
  });
}

async function readyUrl(process: ChildProcessWithoutNullStreams): Promise<string> {
  const lines = createInterface({ input: process.stdout });
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      clearTimeout(timeout);
      process.off("error", onError);
      process.off("exit", onExit);
      lines.off("line", onLine);
      lines.close();
    };
    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const onError = (error: Error) => fail(error);
    const onExit = (code: number | null, signal: NodeJS.Signals | null) =>
      fail(new Error(`packaged recorder exited before startup (${code ?? signal ?? "unknown"})`));
    const onLine = (line: string) => {
      const match = /^Agentic Saga recorder: (http:\/\/127\.0\.0\.1:\d+)$/.exec(line);
      if (!match?.[1]) return;
      if (settled) return;
      settled = true;
      cleanup();
      resolve(match[1]);
    };
    const timeout = setTimeout(() => fail(new Error("packaged recorder did not start")), 10_000);
    process.once("error", onError);
    process.once("exit", onExit);
    lines.on("line", onLine);
  });
}

function hasExited(process: ChildProcessWithoutNullStreams): boolean {
  return process.exitCode !== null || process.signalCode !== null;
}

async function waitForExit(
  process: ChildProcessWithoutNullStreams,
  timeoutMs: number,
): Promise<boolean> {
  if (hasExited(process)) return true;
  return new Promise((resolve) => {
    let settled = false;
    const finish = (exited: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      process.off("exit", onExit);
      resolve(exited);
    };
    const onExit = () => finish(true);
    const timeout = setTimeout(() => finish(false), timeoutMs);
    process.once("exit", onExit);
    if (hasExited(process)) finish(true);
  });
}

function closeRecorderPipes(process: ChildProcessWithoutNullStreams): void {
  process.stdin.destroy();
  process.stdout.destroy();
  process.stderr.destroy();
}

async function stopRecorder(process: ChildProcessWithoutNullStreams): Promise<void> {
  try {
    for (const signal of STOP_SIGNALS) {
      if (hasExited(process)) return;
      process.kill(signal);
      if (await waitForExit(process, STOP_GRACE_MS)) return;
    }
    throw new Error("packaged recorder did not exit after SIGKILL");
  } finally {
    closeRecorderPipes(process);
  }
}

function externalPattern(origin: string): RegExp {
  const escaped = origin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^(?!${escaped}/)https?://`);
}

async function recordNetwork(page: Page, origin: string): Promise<NetworkEvidence> {
  const evidence: NetworkEvidence = { externalOrigins: [], tracePaths: [], webSockets: [] };
  page.on("request", (request) => recordRequest(request.url(), origin, evidence));
  page.on("websocket", (socket) => evidence.webSockets.push(socket.url()));
  await page.route(externalPattern(origin), async (route) => {
    evidence.externalOrigins.push(new URL(route.request().url()).origin);
    await route.abort("blockedbyclient");
  });
  await page.routeWebSocket(/wss?:\/\//, async (socket) => {
    evidence.webSockets.push(socket.url());
    await socket.close();
  });
  return evidence;
}

function recordRequest(rawUrl: string, origin: string, evidence: NetworkEvidence): void {
  const url = new URL(rawUrl);
  if (url.origin !== origin) evidence.externalOrigins.push(url.origin);
  else if (url.pathname.startsWith("/traces/")) evidence.tracePaths.push(url.pathname);
}

async function proveHumanRequiredQuiescence(page: Page): Promise<void> {
  await page.getByRole("tab", { name: "Proof" }).click();
  await expect(page.getByText(/recorded stop is quiescent, not terminal proof/i)).toBeVisible();
  await expect(page.getByRole("button", { name: "Play replay" })).toBeDisabled();
  const position = await page.getByText(/Event \d+ of \d+/).textContent();
  await page.clock.fastForward(800);
  await expect(page.getByText(position ?? "missing position", { exact: true })).toBeVisible();
}

function p95(samples: readonly number[]): number {
  const ordered = [...samples].sort((first, second) => first - second);
  return ordered[Math.ceil(ordered.length * 0.95) - 1] ?? Number.POSITIVE_INFINITY;
}

async function writeBrowserMeasurements(samples: readonly number[]): Promise<void> {
  const output = process.env.AGENTIC_SAGA_BROWSER_METRICS;
  if (output) await writeFile(output, `${JSON.stringify({ samples_ms: samples })}\n`);
  process.stdout.write(`BROWSER_FRESH_NAVIGATION_P95_MS=${p95(samples).toFixed(3)}\n`);
}

for (const [scenario, outcome, evidenceLabel] of runs) {
  test(`wheel-installed ${scenario} renders its recorded outcome without egress`, async ({
    page,
  }) => {
    if (scenario === "compensation-failure") await page.clock.install();
    const server = launchRecorder(scenario);
    try {
      const url = await readyUrl(server);
      const network = await recordNetwork(page, new URL(url).origin);
      await page.goto(url);
      await expect(page.getByRole("heading", { name: "Saga Flight Recorder" })).toBeVisible();
      await expect(page.getByText(outcome, { exact: true }).first()).toBeVisible();
      await expect(page.getByText(evidenceLabel, { exact: true }).first()).toBeVisible();
      if (scenario === "compensation-failure") await proveHumanRequiredQuiescence(page);
      expect(new Set(network.tracePaths)).toEqual(
        new Set(["/traces/index.json", `/traces/${scenario}.json`]),
      );
      expect(network.externalOrigins).toEqual([]);
      expect(network.webSockets).toEqual([]);
    } finally {
      await stopRecorder(server);
    }
  });
}

test("wheel-installed recorder meets the warm-browser fresh-page navigation budget", async ({
  browser,
}) => {
  const samples: number[] = [];
  for (let sample = 0; sample < 10; sample += 1) {
    const server = launchRecorder("happy-path");
    const page = await browser.newPage();
    try {
      const url = await readyUrl(server);
      const started = performance.now();
      await page.goto(url);
      await expect(page.getByText("Succeeded verified", { exact: true }).first()).toBeVisible();
      samples.push(performance.now() - started);
    } finally {
      await page.close();
      await stopRecorder(server);
    }
  }
  await writeBrowserMeasurements(samples);
  expect(p95(samples)).toBeLessThanOrEqual(2_000);
});
