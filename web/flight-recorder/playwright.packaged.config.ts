import { defineConfig, devices } from "@playwright/test";

// biome-ignore lint/style/noDefaultExport: Playwright loads its configuration as a default export.
export default defineConfig({
  forbidOnly: true,
  fullyParallel: false,
  outputDir: "../../test-results/flight-recorder-packaged",
  projects: [
    {
      name: "packaged-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { height: 900, width: 1440 } },
    },
  ],
  reporter: "line",
  retries: 0,
  testDir: "./e2e-packaged",
  timeout: 30_000,
  use: {
    colorScheme: "light",
    locale: "en-US",
    screenshot: "only-on-failure",
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  workers: 1,
});
