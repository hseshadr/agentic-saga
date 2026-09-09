import { defineConfig, devices } from "@playwright/test";

const LOOPBACK_URL = "http://127.0.0.1:4178";

// biome-ignore lint/style/noDefaultExport: Playwright loads its configuration as a default export.
export default defineConfig({
  forbidOnly: Boolean(process.env.CI),
  fullyParallel: false,
  outputDir: "../../test-results/flight-recorder",
  projects: [
    {
      grepInvert: /@mobile/,
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { height: 900, width: 1440 } },
    },
    {
      name: "mobile",
      use: {
        ...devices["Desktop Chrome"],
        hasTouch: true,
        isMobile: true,
        viewport: { height: 844, width: 390 },
      },
    },
  ],
  reporter: "line",
  retries: 0,
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: LOOPBACK_URL,
    colorScheme: "light",
    locale: "en-US",
    screenshot: "only-on-failure",
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "pnpm exec vite --host 127.0.0.1 --port 4178 --strictPort",
    reuseExistingServer: false,
    timeout: 30_000,
    url: LOOPBACK_URL,
  },
  ...(process.env.CI ? { workers: 1 } : {}),
});
