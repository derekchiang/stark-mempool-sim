// Render checks for viewer.html. `npx playwright test` (or npm run
// test:render) builds a fixture trace, serves it with serve.py, and asserts
// rendering *properties* -- ink on every panel, a tab that really switches, a
// header that reaches the drawing -- rather than golden screenshots, which
// break on every browser update and train people to rubber-stamp the diff.
const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tools",
  testMatch: "**/*.spec.js",
  timeout: 30_000,
  // The fixture is deterministic, so a failure is real; retries would only
  // let a race hide behind them.
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:8123",
    viewport: { width: 1600, height: 1000 },
  },
  webServer: {
    command: "sh tools/render_server.sh",
    url: "http://127.0.0.1:8123/viewer.html",
    reuseExistingServer: false,
  },
});
