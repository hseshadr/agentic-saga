import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./app";
import { ScenarioRepository } from "./scenarios/repository";
import { loadIndexFixture } from "./test/load-index-fixture";

const TRACE_ROOT = resolve(process.cwd(), "../../examples/ecommerce/flight-recorder/traces");

function fixtureFetcher(index: unknown = loadIndexFixture()) {
  return vi.fn<typeof fetch>().mockImplementation(async (url) => {
    const name = new URL(String(url)).pathname.split("/").at(-1) ?? "";
    return response(
      name === "index.json"
        ? JSON.stringify(index)
        : readFileSync(resolve(TRACE_ROOT, name), "utf8"),
    );
  });
}

function response(body: string, status = 200): Response {
  return new Response(body, { headers: { "content-type": "application/json" }, status });
}

describe("App", () => {
  it("loads a generic index and renders its verified real trace", async () => {
    const user = userEvent.setup();
    const fetcher = fixtureFetcher();
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    render(<App repository={repository} />);

    expect(screen.getByText(/Loading recorded evidence/)).toBeVisible();
    expect(await screen.findByText("Completed safely")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /refund cannot be verified/i }));
    expect(await screen.findByText("Needs human review")).toBeInTheDocument();
    expect(await screen.findByText("4 / 4 use cases passed")).toBeVisible();
  });

  it("opens the catalog default while keeping all scenarios available", async () => {
    const index = { ...(loadIndexFixture() as object), default_run_id: "compensation-failure" };
    const fetcher = fixtureFetcher(index);
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    render(<App repository={repository} />);

    expect(await screen.findByText("Needs human review")).toBeVisible();
    expect(screen.getByText(/4 recorded scenarios/)).toBeVisible();
    expect(String(fetcher.mock.calls[1]?.[0])).toMatch(/compensation-failure\.json$/);
  });

  it("shows a safe, actionable load failure", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response("private body", 500));
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    render(<App repository={repository} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Trace index could not be loaded");
    expect(screen.getByRole("alert")).not.toHaveTextContent("private body");
  });

  it("distinguishes a selected trace failure from an index failure", async () => {
    const user = userEvent.setup();
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(response(JSON.stringify(loadIndexFixture())))
      .mockResolvedValueOnce(response("untrusted provider body", 503));
    const repository = new ScenarioRepository(new URL("https://recorder.test/traces/"), fetcher);

    render(<App repository={repository} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("RunTrace could not be loaded");
    expect(screen.getByRole("alert")).not.toHaveTextContent("untrusted provider body");
    expect(screen.getByRole("navigation", { name: "Run trajectory" })).toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveFocus();
    await user.tab();
    expect(alert).not.toHaveFocus();
  });
});
