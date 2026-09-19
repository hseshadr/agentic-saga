import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type TestInfo, test } from "@playwright/test";
import { installTraceRoutes, type NetworkEvidence } from "./install-trace-routes";

const runs = [
  [0, "Completed safely", "/traces/happy-path.json", "Terminal assigned"],
  [1, "Completed safely", "/traces/lost-response.json", "Reconciliation recorded"],
  [2, "Safely undone", "/traces/business-failure.json", "Compensation started"],
  [3, "Waiting for a human", "/traces/compensation-failure.json", "Human required"],
] as const;

async function openRecorder(page: Page): Promise<NetworkEvidence> {
  const evidence = await installTraceRoutes(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Agentic Saga Replay" })).toBeVisible();
  return evidence;
}

async function selectRun(page: Page, index: number, outcome: string): Promise<void> {
  await page
    .getByRole("navigation", { name: "Run trajectory" })
    .getByRole("button")
    .nth(index)
    .click();
  await expect(page.getByText(outcome, { exact: true }).first()).toBeVisible();
}

async function expectBusinessFailureEvidence(page: Page): Promise<void> {
  await page.getByRole("button", { name: /\. Compensation started$/ }).click();
  await expect(page.getByRole("complementary", { name: "Recorded evidence" })).toContainText(
    "compensating",
  );
  await page
    .getByRole("button", { name: /\. Invariant evaluated$/ })
    .first()
    .click();
  await expect(page.getByRole("complementary", { name: "Recorded evidence" })).toContainText(
    '"verified": false',
  );
  await page.getByRole("tab", { name: "Ledger" }).click();
  await page.getByLabel("Search recorded fields").fill("charge_payment");
  const charge = page.getByRole("row").filter({ hasText: "Effect outcome recorded" });
  await charge.getByRole("button", { name: /Inspect event/ }).click();
  await expect(page.getByRole("complementary", { name: "Recorded evidence" })).toContainText(
    "effect_confirmed",
  );
}

async function expectNoOverflow(page: Page): Promise<void> {
  const width = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(width.scroll).toBeLessThanOrEqual(width.client + 1);
}

async function attachScreenshot(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  const path = testInfo.outputPath(`${name}.png`);
  await page.screenshot({ fullPage: true, path });
  await testInfo.attach(name, { contentType: "image/png", path });
}

async function expectResponsiveFlow(page: Page, width: number): Promise<void> {
  const board = await page.getByRole("region", { name: "Causal flight path" }).boundingBox();
  const inspector = await page
    .getByRole("complementary", { name: "Recorded evidence" })
    .boundingBox();
  if (!board || !inspector) throw new Error("recorder panels must have browser geometry");
  if (width > 1180) expect(inspector.x).toBeGreaterThan(board.x);
  else expect(inspector.y).toBeGreaterThan(board.y);
}

async function boundaryColors(
  borderElement: ReturnType<Page["locator"]>,
  backgroundElement: ReturnType<Page["locator"]>,
): Promise<readonly [string, string]> {
  const border = await borderElement.evaluate((element) => getComputedStyle(element).borderColor);
  const background = await backgroundElement.evaluate(
    (element) => getComputedStyle(element).backgroundColor,
  );
  return [border, background];
}

function contrastRatio(first: string, second: string): number {
  const values = [relativeLuminance(first), relativeLuminance(second)].sort((a, b) => b - a);
  const [lighter, darker] = values;
  if (lighter === undefined || darker === undefined) throw new Error("contrast values unavailable");
  return (lighter + 0.05) / (darker + 0.05);
}

function relativeLuminance(color: string): number {
  const channels = color
    .match(/[\d.]+/g)
    ?.slice(0, 3)
    .map(Number);
  if (channels?.length !== 3) throw new Error("browser color unavailable");
  const [red = 0, green = 0, blue = 0] = channels.map(linearChannel);
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function linearChannel(channel: number): number {
  const value = channel / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

test("each real trace renders its recorded safety outcome without external requests", async ({
  page,
}) => {
  const evidence = await openRecorder(page);
  for (const [index, outcome, path, evidenceLabel] of runs) {
    await selectRun(page, index, outcome);
    await expect(page.getByText(evidenceLabel, { exact: true }).first()).toBeVisible();
    if (path === "/traces/business-failure.json") await expectBusinessFailureEvidence(page);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  }

  expect(new Set(evidence.tracePaths)).toEqual(
    new Set(["/traces/index.json", ...runs.map((run) => run[2])]),
  );
  expect(evidence.externalOrigins).toEqual([]);
  expect(evidence.consoleErrors).toEqual([]);
  expect(evidence.pageErrors).toEqual([]);
});

test("keyboard replay, tabs, inspection, and focus return preserve context", async ({ page }) => {
  await page.clock.install();
  await openRecorder(page);
  const initialPosition = await page.getByText(/Event \d+ of \d+/).textContent();
  await page.clock.fastForward(800);
  await expect(
    page.getByText(initialPosition ?? "missing position", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: "Skip to flight recorder" });
  await expect(skipLink).toBeFocused();
  await expect(skipLink).toHaveCSS("outline-style", "solid");
  await page.keyboard.press("Enter");
  await expect(page.locator("#flight-recorder-content")).toBeFocused();
  await page.keyboard.press("Home");
  await expect(page.getByText(/Event 1 of/)).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByText(/Event 2 of/)).toHaveAttribute("aria-live", "off");
  await page.keyboard.press("Home");
  await page.keyboard.press(" ");
  await expect(page.getByRole("button", { name: "Pause replay" })).toBeVisible();
  await page.clock.fastForward(701);
  await expect(page.getByText(/Event 2 of/)).toBeVisible({ timeout: 1_200 });
  await page.keyboard.press(" ");
  await expect(page.getByRole("button", { name: "Play replay" })).toBeVisible();
  const story = page.getByRole("tab", { name: "Story" });
  await story.focus();
  await page.keyboard.press("ArrowRight");
  const ledgerTab = page.getByRole("tab", { name: "Ledger" });
  await expect(ledgerTab).toBeFocused();
  await expect(ledgerTab).toHaveCSS("outline-style", "solid");
  await expect(page.getByRole("tabpanel", { name: "Ledger" })).toBeVisible();
  const inspect = page.getByRole("button", { name: /Inspect event/ }).first();
  await inspect.click();
  await expect(page.getByRole("complementary", { name: "Recorded evidence" })).toBeFocused();
  await story.click();
  await expect(story).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "Return to selected event" }).click();
  await expect(ledgerTab).toHaveAttribute("aria-selected", "true");
  await expect(inspect).toBeFocused();
});

test("watch from start reveals the ecommerce Saga at a human pace", async ({ page }) => {
  await page.clock.install();
  await openRecorder(page);
  await selectRun(page, 2, "Safely undone");
  const replay = await page.evaluate(async () => {
    const response = await fetch("/traces/business-failure.json");
    const trace = (await response.json()) as {
      events: Array<{ event_type: string }>;
    };
    return {
      compensationPosition:
        trace.events.findIndex(({ event_type }) => event_type === "compensation_started") + 1,
      eventCount: trace.events.length,
      firstPausePosition:
        trace.events.findIndex(({ event_type }) =>
          ["compensation_started", "invariant_evaluated", "reconciliation_recorded"].includes(
            event_type,
          ),
        ) + 1,
    };
  });

  await page.getByRole("button", { name: "Watch from start" }).click();
  await expect(page.getByText(`Event 1 of ${replay.eventCount}`)).toBeVisible();
  await expect(page.getByText("The agent is reading the order goal")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pause replay" })).toBeVisible();

  await page.clock.fastForward(600);
  await expect(page.getByText(`Event 1 of ${replay.eventCount}`)).toBeVisible();
  await page.clock.fastForward(100);
  await expect(page.getByText(`Event 2 of ${replay.eventCount}`)).toBeVisible();

  for (let event = 3; event <= replay.firstPausePosition; event += 1) {
    await page.clock.fastForward(700);
    await expect(page.getByText(`Event ${event} of ${replay.eventCount}`)).toBeVisible();
  }
  await expect(page.getByRole("button", { name: "Play replay" })).toBeVisible();
  if (replay.firstPausePosition < replay.compensationPosition) {
    await page.getByRole("button", { name: "Play replay" }).click();
    for (
      let event = replay.firstPausePosition + 1;
      event <= replay.compensationPosition;
      event += 1
    ) {
      await page.clock.fastForward(700);
      await expect(page.getByText(`Event ${event} of ${replay.eventCount}`)).toBeVisible();
    }
  }
  await expect(
    page.getByText(`Event ${replay.compensationPosition} of ${replay.eventCount}`),
  ).toBeVisible();
  await expect(page.getByText("A later step failed—starting safe undo actions")).toBeVisible();
  await expect(page.getByRole("button", { name: "Play replay" })).toBeVisible();
});

test("all evidence views are axe-clean and retain semantic tabs", async ({ page }) => {
  await openRecorder(page);
  for (const name of ["Story", "Ledger", "Proof"]) {
    const tab = page.getByRole("tab", { name });
    const panelId = await tab.getAttribute("aria-controls");
    if (!panelId) throw new Error("each evidence tab must identify its panel");
    await tab.click();
    const panel = page.getByRole("tabpanel", { name });
    await expect(panel).toBeVisible();
    await expect(page.locator(`#${panelId}`)).toHaveAttribute("role", "tabpanel");
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  }
});

test("responsive layouts preserve one readable stream without page overflow", async ({
  page,
}, testInfo) => {
  for (const width of [1440, 1100, 900, 390, 320]) {
    await page.setViewportSize({ height: 900, width });
    await openRecorder(page);
    await expectNoOverflow(page);
    await expectResponsiveFlow(page, width);
    await expect(page.getByRole("list", { name: "Ledger events in causal order" })).toBeVisible();
    if (width <= 390) {
      await expect(page.getByRole("list", { name: "Ledger events in causal order" })).toHaveCSS(
        "display",
        "block",
      );
    }
    await page.getByRole("tab", { name: "Ledger" }).click();
    await expect(page.getByRole("table", { name: "Recorded ledger" })).toBeVisible();
    if (width <= 390) {
      await expect(
        page.getByRole("table", { name: "Recorded ledger" }).locator("tbody tr").first(),
      ).toHaveCSS("display", "block");
    }
    await expectNoOverflow(page);
    await attachScreenshot(page, testInfo, `ledger-${width}`);
    await page.getByRole("tab", { name: "Proof" }).click();
    await expectNoOverflow(page);
    if (width <= 390) {
      await expect(
        page.getByRole("complementary", { name: "Recorded evidence" }).locator("pre").first(),
      ).toHaveCSS("overflow-x", "auto");
    }
  }
});

test("200 percent text zoom reflows every view without clipping the page", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ height: 900, width: 720 });
  await openRecorder(page);
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "200%";
  });
  for (const name of ["Story", "Ledger", "Proof"]) {
    await page.getByRole("tab", { name }).click();
    await expectNoOverflow(page);
  }
  const signals = page
    .getByRole("list", { name: "Ledger events in causal order" })
    .getByRole("button");
  const gaps = await signals.evaluateAll((buttons) =>
    buttons.slice(1).map((button, index) => {
      const previous = buttons[index];
      if (!previous) throw new Error("a previous signal must exist");
      return button.getBoundingClientRect().top - previous.getBoundingClientRect().bottom;
    }),
  );
  expect(Math.min(...gaps)).toBeGreaterThanOrEqual(0);
  await attachScreenshot(page, testInfo, "zoom-200-percent");
});

test("essential control boundaries retain non-text contrast", async ({ page }) => {
  await openRecorder(page);
  const trajectory = page.getByRole("navigation", { name: "Run trajectory" });
  const run = trajectory.getByRole("button").first();
  await page.getByRole("tab", { name: "Ledger" }).click();
  const search = page.getByLabel("Search recorded fields");
  const [runBorder, runBackground] = await boundaryColors(run, trajectory);
  const [searchBorder, searchBackground] = await boundaryColors(search, search);
  expect(contrastRatio(runBorder, runBackground)).toBeGreaterThanOrEqual(3);
  expect(contrastRatio(searchBorder, searchBackground)).toBeGreaterThanOrEqual(3);
});

test("reduced motion prevents autoplay while retaining manual replay", async ({ page }) => {
  await page.clock.install();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openRecorder(page);
  const play = page.getByRole("button", { name: "Play replay" });
  await expect(play).toBeDisabled();
  await expect(page.getByRole("button", { name: "Watch from start" })).toBeDisabled();
  await expect(page.getByText(/motion preference is active/i)).toBeVisible();
  const durationMs = await page.getByRole("button", { name: "Next event" }).evaluate((button) => {
    button.style.transitionDuration = "2s";
    const value = getComputedStyle(button).transitionDuration;
    return Number.parseFloat(value) * (value.endsWith("ms") ? 1 : 1_000);
  });
  expect(durationMs).toBeLessThanOrEqual(0.01);
  await page.getByRole("button", { name: "Restart replay" }).click();
  await page.clock.fastForward(800);
  await expect(page.getByText(/Event 1 of/)).toBeVisible();
  await page.getByRole("button", { name: "Next event" }).click();
  await expect(page.getByText(/Event 2 of/)).toBeVisible();
});

test("human-required evidence remains quiescent and distinct from terminal proof", async ({
  page,
}) => {
  await page.clock.install();
  await openRecorder(page);
  await selectRun(page, 3, "Waiting for a human");
  await expect(page.getByText("Paused safely for human review", { exact: true })).toBeVisible();
  await expect(page.getByRole("listitem", { name: "Refund payment: Needs a human" })).toBeVisible();
  await expect(page.getByRole("listitem", { name: "Release stock: Waiting" })).toBeVisible();
  await page.getByRole("tab", { name: "Proof" }).click();
  await expect(page.getByText(/recorded stop is quiescent, not terminal proof/i)).toBeVisible();
  await expect(page.getByRole("button", { name: "Play replay" })).toBeDisabled();
  const position = await page.getByText(/Event \d+ of \d+/).textContent();
  await page.clock.fastForward(800);
  await expect(page.getByText(position ?? "missing position", { exact: true })).toBeVisible();
});

test("@mobile coarse-pointer controls meet the 44-pixel target", async ({ page }) => {
  await openRecorder(page);
  for (const control of await page.getByRole("button").all()) {
    if (!(await control.isVisible())) continue;
    const box = await control.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
    expect(box?.width).toBeGreaterThanOrEqual(44);
  }
  await page.getByRole("tab", { name: "Ledger" }).click();
  for (const field of await page.locator("input, select").all()) {
    const box = await field.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
});
