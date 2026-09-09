import type { Page } from "@playwright/test";

export interface NetworkEvidence {
  readonly consoleErrors: string[];
  readonly externalOrigins: string[];
  readonly pageErrors: string[];
  readonly tracePaths: string[];
}

export async function installTraceRoutes(page: Page): Promise<NetworkEvidence> {
  const evidence: NetworkEvidence = {
    consoleErrors: [],
    externalOrigins: [],
    pageErrors: [],
    tracePaths: [],
  };
  page.on("console", (message) => {
    if (message.type() === "error") evidence.consoleErrors.push(message.text());
  });
  page.on("pageerror", () => evidence.pageErrors.push("uncaught page error"));
  page.on("websocket", (socket) => recordExternal(socket.url(), evidence));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (!isRecorderOrigin(url)) {
      evidence.externalOrigins.push(url.origin);
      await route.abort("blockedbyclient");
      return;
    }
    if (url.pathname.startsWith("/traces/")) evidence.tracePaths.push(url.pathname);
    await route.continue();
  });
  return evidence;
}

function recordExternal(rawUrl: string, evidence: NetworkEvidence): void {
  const url = new URL(rawUrl);
  if (!isRecorderOrigin(url)) evidence.externalOrigins.push(url.origin);
}

function isRecorderOrigin(url: URL): boolean {
  return (
    url.hostname === "127.0.0.1" &&
    url.port === "4178" &&
    (url.protocol === "http:" || url.protocol === "ws:")
  );
}
