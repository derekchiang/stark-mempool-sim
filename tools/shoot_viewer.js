// Regenerate the viewer screenshots in docs/images from the deterministic
// fixture.
//
//     node tools/shoot_viewer.js
//
// The pictures in docs/explainer.md are of this fixture and nothing else, so
// that they can be regenerated rather than re-shot by hand when viewer.html
// changes: a screenshot taken from a live run is of a run nobody can repeat.
// The fixture is the one tools/check_viewer.mjs reduces against and
// tools/check_viewer_render.spec.js draws, served the same way. The replay is
// driven the way the render checks drive it, by freezing the speed and moving
// the replay clock, never by waiting on the wall clock.
//
// Three pictures of one transaction, the first one auto-follow settles on:
// the network page just before it settles, while the nodes still differ in
// what they know; then the charts and transaction pages at the end of the
// run, where the story is complete. Regenerating must give the same bytes,
// so the poll status line, which carries a wall-clock time, is hidden and the
// theme is pinned.
//
// Needs the render-check toolchain: `npm ci` and
// `npx playwright install chromium`.

const { spawn } = require("child_process");
const path = require("path");
const { chromium } = require("@playwright/test");

const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(ROOT, "docs", "images");
const URL = "http://127.0.0.1:8123/viewer.html";
// How long before settlement the network page is shot. Inside one slot of the
// fixture, so the transaction is still spreading and its classes are mixed.
const BEFORE_SETTLE_MS = 120;

async function waitFor(url, attempts) {
  for (let i = 0; i < attempts; i++) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch (error) {
      // Not up yet.
    }
    await new Promise((done) => setTimeout(done, 250));
  }
  throw new Error("render server did not come up at " + url);
}

async function settle(page) {
  // A control changed through the page keeps focus, and a focus ring is a
  // few dozen pixels that differ between two otherwise identical runs.
  await page.evaluate(() => {
    if (document.activeElement) document.activeElement.blur();
    // The slot-boundary flash decays per animation frame, not per simulated
    // millisecond, so with the clock frozen it is the one thing that still
    // depends on how many frames have passed.
    state.boundaryFlash = 0;
  });
  // Park the pointer off every control, and give the native form controls
  // more than the two frames the render checks need: a select's focus and
  // active states fade out over frames, and were the last few dozen pixels
  // that differed between two otherwise identical runs.
  await page.mouse.move(1, 1);
  await page.evaluate(() => new Promise((done) => {
    let left = 6;
    const tick = () => (--left ? requestAnimationFrame(tick) : done());
    requestAnimationFrame(tick);
  }));
}

async function load(page) {
  await page.goto(URL);
  await page.waitForFunction(() => feed.events.length > 100);
  await page.evaluate(() => {
    document.documentElement.setAttribute("data-theme", "light");
    document.getElementById("status").hidden = true;
    player.speed = 0;
    // The page starts replaying at real speed the moment it loads, and the
    // reducer cannot run backwards; on a slow load the clock could already
    // be past the earliest instant wanted below. Start again from zero
    // with the speed frozen.
    player.hardReset();
  });
}

// Advance the replay clock to `at` and wait until every event up to it has
// been reduced.
async function advanceTo(page, at) {
  await page.evaluate((t) => { player.simT = t; }, at);
  await page.waitForFunction((t) =>
    player.cursor === feed.events.length || feed.events[player.cursor].t > t, at);
  await settle(page);
}

async function main() {
  const server = spawn("sh", [path.join(ROOT, "tools", "render_server.sh")],
                       { stdio: ["ignore", "ignore", "inherit"] });
  try {
    await waitFor(URL, 120);
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

    // First pass: replay to the end and learn which transaction settled
    // first and when. The end of the trace is its last event, not a
    // sentinel, so the ticker reads the run's own time.
    await load(page);
    const end = await page.evaluate(() => feed.events[feed.events.length - 1].t + 1);
    await advanceTo(page, end);
    const first = await page.evaluate(() => {
      const [tx, at] = state.settledAt.entries().next().value;
      return { tx: tx, at: at };
    });

    // Second pass: a fresh replay, stopped just before that settlement, with
    // the transaction pinned so all three pages describe it.
    await load(page);
    await advanceTo(page, first.at - BEFORE_SETTLE_MS);
    await page.selectOption("#follow", "pinned");
    await page.selectOption("#followed", String(first.tx));
    await settle(page);

    await page.click("#tab-network");
    await page.selectOption("#netmetric", "knowledge");
    await page.selectOption("#netlayout", "ring");
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "viewer-network.png") });

    await advanceTo(page, end);
    await page.click("#tab-transaction");
    await page.selectOption("#netlayout", "rows");
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "viewer-transaction.png") });

    await page.click("#tab-charts");
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "viewer-charts.png"),
                            fullPage: true });

    await browser.close();
    console.log("wrote three screenshots of tx " + first.tx + " to " +
                path.relative(ROOT, OUT));
  } finally {
    server.kill();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
