import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { expect, it, vi } from "vitest";
import { App } from "./app";
import { ScenarioRepository } from "./scenarios/repository";
import { loadIndexFixture } from "./test/load-index-fixture";

const TRACE = readFileSync(
  resolve(process.cwd(), "../../examples/ecommerce/flight-recorder/traces/happy-path.json"),
  "utf8",
);

function response(body: string, status = 200): Response {
  return new Response(body, { headers: { "content-type": "application/json" }, status });
}

function loadedRepository(): ScenarioRepository {
  const fetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(response(JSON.stringify(loadIndexFixture())))
    .mockResolvedValueOnce(response(TRACE));
  return new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);
}

async function expectAxeClean(container: HTMLElement): Promise<void> {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  expect(result.violations).toEqual([]);
}

it("keeps loading and invalid-trace states axe-clean", async () => {
  const pending = vi.fn<typeof fetch>(() => new Promise<Response>(() => undefined));
  const loading = render(
    <App repository={new ScenarioRepository(new URL("https://recorder.test/"), pending)} />,
  );
  await expectAxeClean(loading.container);
  loading.unmount();
  const invalidFetcher = vi
    .fn<typeof fetch>()
    .mockResolvedValueOnce(response(JSON.stringify(loadIndexFixture())))
    .mockResolvedValueOnce(response("{}"));
  const invalid = render(
    <App repository={new ScenarioRepository(new URL("https://recorder.test/"), invalidFetcher)} />,
  );
  await screen.findByRole("alert");
  await expectAxeClean(invalid.container);
});

it("keeps loaded and empty-filter states axe-clean", async () => {
  const user = userEvent.setup();
  const loaded = render(<App repository={loadedRepository()} />);
  await screen.findByText("Completed safely");
  await expectAxeClean(loaded.container);
  await user.click(screen.getByRole("tab", { name: "Ledger" }));
  await user.type(screen.getByLabelText("Search recorded fields"), "not-a-recorded-value");
  await screen.findByText("No visible ledger events match these filters.");
  await expectAxeClean(loaded.container);
}, 10_000);
