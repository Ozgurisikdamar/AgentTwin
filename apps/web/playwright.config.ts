import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against a running stack (`make dev`), never against mocks:
 * a real agent run travels SDK → OTel Collector → trace-service →
 * control plane → web BFF → browser.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? `http://localhost:${process.env.WEB_HOST_PORT ?? "3000"}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } },
  ],
});
