// The rendering half of the viewer harness.
//
// tools/check_viewer.mjs proves the reducers compute the right numbers; this
// file proves the numbers reach the screen. The three bugs that motivated it
// were all "the data was right and the drawing was wrong": a heatmap that read
// as empty while holding exact data, a tab that appeared dead because an
// author `display` beat `[hidden]`, and a config bound that never reached the
// header so a panel silently drew nothing. No assertion about numbers can see
// any of those, so the assertions here are about pixels and visibility --
// *properties* (there is ink; the view changed; the bound arrived), never
// golden images, which break on every browser update and train people to
// rubber-stamp the diff.
//
// The viewer replays a trace on a wall clock. Tests fast-forward it
// deterministically instead of waiting: freeze the speed, jump `player.simT`
// past the end, and wait for the cursor to consume every event.

const { test, expect } = require("@playwright/test");

// Below this fraction of changed pixels a panel is "blank" for our purposes.
// Axes, labels and at least one series always exceed it by an order of
// magnitude; an empty canvas or a background-only draw never reaches it.
const MIN_INK = 0.005;

async function loadFinished(page) {
  await page.goto("/viewer.html");
  await page.waitForFunction(() => feed.events.length > 100);
  await page.evaluate(() => {
    player.speed = 0;
    player.simT = Number.MAX_SAFE_INTEGER;
  });
  await page.waitForFunction(() => player.cursor === feed.events.length);
  // One more animation frame so every panel has drawn the final state.
  await page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
}

// Fraction of a canvas's pixels that differ from its top-left corner. The
// corner is background on every panel, so this is "how much ink is on it"
// without knowing or caring what the chart should look like.
function inkFraction(page, canvasId) {
  return page.evaluate((id) => {
    const canvas = document.getElementById(id);
    if (!canvas || canvas.width === 0 || canvas.height === 0) return -1;
    const context = canvas.getContext("2d");
    const data = context.getImageData(0, 0, canvas.width,
                                      canvas.height).data;
    const background = [data[0], data[1], data[2]];
    let ink = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (Math.abs(data[i] - background[0]) > 24
          || Math.abs(data[i + 1] - background[1]) > 24
          || Math.abs(data[i + 2] - background[2]) > 24) {
        ink += 1;
      }
    }
    return ink / (canvas.width * canvas.height);
  }, canvasId);
}

test("the page loads and replays with a clean console", async ({ page }) => {
  const problems = [];
  page.on("pageerror", (error) => problems.push(String(error)));
  page.on("console", (message) => {
    // The favicon 404 is the server being minimal, not the page being wrong.
    if (message.type() === "error" && !message.text().includes("favicon")) {
      problems.push(message.text());
    }
  });
  await loadFinished(page);
  expect(problems).toEqual([]);
});

test("every timeline panel has ink on it", async ({ page }) => {
  await loadFinished(page);
  const panelCanvases = await page.evaluate(() =>
    Array.from(document.querySelectorAll("canvas[id^='cv-']"))
      .map((canvas) => canvas.id));
  // The grids hold ten panels today; fewer than six means the page half
  // failed to build, and asserting a number stops a regression hiding
  // behind "the ones that exist all pass".
  expect(panelCanvases.length).toBeGreaterThanOrEqual(6);
  for (const canvasId of panelCanvases) {
    expect(await inkFraction(page, canvasId), canvasId).toBeGreaterThan(MIN_INK);
  }
});

test("the network tab really switches, both ways", async ({ page }) => {
  await loadFinished(page);
  const before = await page.screenshot();
  await page.click("#tab-network");
  // Semantically switched...
  await expect(page.locator("#network")).toBeVisible();
  await expect(page.locator("#panels")).toBeHidden();
  // ...and *visibly* switched: the dead-tab bug kept both semantics and data
  // correct while the pixels stayed the same, so pixels are the assertion.
  const after = await page.screenshot();
  expect(before.equals(after)).toBe(false);
  // The map itself drew: nodes and edges, not an empty pane.
  expect(await inkFraction(page, "netcv")).toBeGreaterThan(MIN_INK);
  // And back.
  await page.click("#tab-charts");
  await expect(page.locator("#panels")).toBeVisible();
  await expect(page.locator("#network")).toBeHidden();
});

test("clicking a node pins its stats; clicking space unpins", async ({ page }) => {
  await loadFinished(page);
  await page.click("#tab-network");
  await expect(page.locator("#network")).toBeVisible();
  // One frame so netPositions is laid out before we aim at it.
  await page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  const targets = await page.evaluate(() => {
    const box = document.getElementById("netplot").getBoundingClientRect();
    const node = netPositions[0];
    // Somewhere demonstrably empty: the coarse-grid point farthest from
    // every node, so the miss-click cannot land inside a hit radius.
    let empty = [0, 0], farthest = -1;
    for (let x = 10; x < box.width; x += 20) {
      for (let y = 10; y < box.height; y += 20) {
        let nearest = Infinity;
        for (const at of netPositions) {
          if (!at) continue;
          const d = (at[0] - x) ** 2 + (at[1] - y) ** 2;
          if (d < nearest) nearest = d;
        }
        if (nearest > farthest) { farthest = nearest; empty = [x, y]; }
      }
    }
    return { box: { x: box.x, y: box.y },
             node: [node[0], node[1]], empty };
  });
  await page.mouse.click(targets.box.x + targets.node[0],
                         targets.box.y + targets.node[1]);
  await expect(page.locator("#netpin")).toBeVisible();
  await expect(page.locator("#netpin")).toContainText("node 0");
  await page.mouse.click(targets.box.x + targets.empty[0],
                         targets.box.y + targets.empty[1]);
  await expect(page.locator("#netpin")).toBeHidden();
});

test("auto follow moves on when its transaction settles", async ({ page }) => {
  await page.goto("/viewer.html");
  await page.waitForFunction(() => feed.events.length > 100);
  const settle = (t) => page.evaluate((at) => {
    player.speed = 0; player.simT = at;
  }, t).then(() => page.waitForFunction((at) =>
    feed.events[player.cursor] === undefined || feed.events[player.cursor].t > at, t))
    .then(() => page.evaluate(() => new Promise((done) => {
      requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(done)));
    })));
  await settle(100);
  const first = await page.evaluate(() => selectedFollowed());
  expect(first).not.toBeNull();
  // Past the first block: the first transaction settled, later ones are born
  // and not yet settled, so auto must have moved to one of those.
  await settle(700);
  const second = await page.evaluate(() => selectedFollowed());
  expect(await page.evaluate((tx) => state.settledAt.has(tx), first)).toBe(true);
  expect(second).not.toBe(first);
  expect(await page.evaluate((tx) => state.settledAt.has(tx), second)).toBe(false);
  await expect(page.locator("#txticker .chip.on")).toContainText("tx " + second);
});

test("the transaction page's rows lie in the order nodes came to know", async ({ page }) => {
  await loadFinished(page);
  await page.click("#tab-transaction");
  await page.selectOption("#netlayout", "rows");
  await page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  const check = await page.evaluate(() => {
    const tx = selectedFollowed();
    const first = firstKnowing(tx);
    let ordered = true, placed = 0;
    const columns = new Set();
    for (let i = 1; i < first.length; i++) {
      const a = netPositions[first[i - 1][1]], b = netPositions[first[i][1]];
      if (!a || !b) continue;
      placed += 1;
      columns.add(Math.round(a[0])); columns.add(Math.round(b[0]));
      if (b[1] <= a[1] || b[0] < a[0]) ordered = false;
    }
    return { ordered, placed, rows: first.length, columns: columns.size };
  });
  expect(check.rows).toBeGreaterThan(1);
  expect(check.ordered).toBe(true);
  // Ordered is not enough: a time axis stuck at the left margin keeps every
  // row "in order" while saying nothing -- the times differ, so must x.
  expect(check.columns).toBeGreaterThan(1);
  await expect(page.locator("#netnote")).toContainText("own clock");
});

test("the lanes lie by hop distance from the entry", async ({ page }) => {
  await loadFinished(page);
  await page.click("#tab-transaction");
  await page.selectOption("#netlayout", "lanes");
  await page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  const check = await page.evaluate(() => {
    const tx = selectedFollowed();
    const hops = hopDistancesFrom(spreadEntry(tx));
    const byHop = new Map();
    for (const pair of firstKnowing(tx)) {
      const position = netPositions[pair[1]];
      if (!position) continue;
      const hop = hops.get(pair[1]);
      if (!byHop.has(hop)) byHop.set(hop, []);
      byHop.get(hop).push(position[1]);
    }
    const laneHops = [...byHop.keys()].sort((a, b) => a - b);
    let separated = true;
    for (let i = 1; i < laneHops.length; i++) {
      if (Math.max(...byHop.get(laneHops[i - 1])) >=
          Math.min(...byHop.get(laneHops[i]))) separated = false;
    }
    return { lanes: laneHops.length, separated };
  });
  expect(check.lanes).toBeGreaterThan(1);
  expect(check.separated).toBe(true);
});

test("the duplicates checkbox puts red on the transaction page and takes it off", async ({ page }) => {
  await loadFinished(page);
  await page.click("#tab-transaction");
  await page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  await expect(page.locator("#netlegend")).toContainText("duplicate delivery");
  const redPixels = () => page.evaluate(() => {
    const canvas = document.getElementById("netcv");
    const data = canvas.getContext("2d").getImageData(0, 0, canvas.width,
                                                       canvas.height).data;
    const want = css("--dup").match(/[0-9a-f]{2}/gi).map((p) => parseInt(p, 16));
    let hits = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (Math.abs(data[i] - want[0]) < 40 && Math.abs(data[i + 1] - want[1]) < 40
          && Math.abs(data[i + 2] - want[2]) < 40) hits += 1;
    }
    return hits;
  });
  const shown = await redPixels();
  expect(shown).toBeGreaterThan(20);
  await page.uncheck("#showdups");
  await page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  expect(await redPixels()).toBeLessThan(shown / 4);
});

test("colouring by knowledge draws the spread tree", async ({ page }) => {
  await loadFinished(page);
  await page.click("#tab-network");
  await page.selectOption("#netmetric", "knowledge");
  await page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  await expect(page.locator("#netlegend")).toContainText("spread via own object");
  // The tree is drawn in a colour no node takes, so its pixels are its own.
  const treePixels = await page.evaluate(() => {
    const canvas = document.getElementById("netcv");
    const data = canvas.getContext("2d").getImageData(0, 0, canvas.width,
                                                       canvas.height).data;
    const want = spreadColour("object").match(/[0-9a-f]{2}/gi)
      .map((pair) => parseInt(pair, 16));
    let hits = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (Math.abs(data[i] - want[0]) < 24 && Math.abs(data[i + 1] - want[1]) < 24
          && Math.abs(data[i + 2] - want[2]) < 24) hits += 1;
    }
    return hits;
  });
  expect(treePixels).toBeGreaterThan(50);
});

test("admission ripples put pixels on the network view", async ({ page }) => {
  await loadFinished(page);
  await page.click("#tab-network");
  // Rewind to just after the last admission, so its ripple is mid-life --
  // then draw the same instant with and without the flash list. Everything
  // else about the frame is identical, so any pixel difference is the ripple.
  await page.evaluate(() => {
    const last = state.netFlashes[state.netFlashes.length - 1];
    player.simT = last.t + 100;
  });
  const settle = () => page.evaluate(() => new Promise((done) => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  await settle();
  const withRipples = await page.locator("#netplot").screenshot();
  await page.evaluate(() => { state.netFlashes = []; });
  await settle();
  const without = await page.locator("#netplot").screenshot();
  expect(withRipples.equals(without)).toBe(false);
});

test("the heat ramp resolves to real colours in both themes", async ({ page }) => {
  // Ink checks cannot tell black-wrong from right-dark, which is how an
  // undefined heat variable drew every heat mark in canvas-default black
  // while every check stayed green. So ask the stylesheet directly, per
  // forced theme.
  await loadFinished(page);
  const resolved = await page.evaluate(() => {
    const out = {};
    for (const theme of ["light", "dark"]) {
      document.documentElement.setAttribute("data-theme", theme);
      const style = getComputedStyle(document.documentElement);
      out[theme] = ["--heat1", "--heat2", "--heat3", "--panel", "--ghost"]
        .map((name) => style.getPropertyValue(name).trim().toLowerCase());
    }
    return out;
  });
  for (const theme of ["light", "dark"]) {
    for (const value of resolved[theme]) {
      expect(value, theme).not.toBe("");
      expect(["#000", "#000000", "rgb(0, 0, 0)"], theme).not.toContain(value);
    }
  }
});

test("the config header reached the page", async ({ page }) => {
  await loadFinished(page);
  // The fixture bounds the send buffer at 400 kB. This bound once lived in
  // metrics() instead of variant(), never reached the trace header, and the
  // buffer-fullness panel silently read `undefined` -- the harness missed it
  // because it read the bound from Python rather than from what the page got.
  const bound = await page.evaluate(() => feed.cfg.egress_queue_bytes);
  expect(bound).toBe(400000);
});

test("the page help opens beside the tabs and closes again", async ({ page }) => {
  await loadFinished(page);
  // Closed by default: the orientation is for a first visit, and a panel
  // that is always open would push every chart down for every visit after.
  await expect(page.locator("#pagehelp")).toBeHidden();
  await page.click("#help-page");
  await expect(page.locator("#pagehelp")).toBeVisible();
  // It names the three pages it orients: a help that lost a tab is stale.
  for (const tab of ["charts", "network", "transaction"]) {
    await expect(page.locator("#pagehelp")).toContainText(tab);
  }
  await page.click("#help-page");
  await expect(page.locator("#pagehelp")).toBeHidden();
});
