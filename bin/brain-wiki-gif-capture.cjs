// brain-wiki-gif-capture.cjs — Playwright frame capture for the wiki GIF.
//
// Driven by bin/brain-wiki-gif. Navigates the served static Quartz wiki and
// writes numbered PNG frames of the four most visual surfaces: the homepage,
// a content note (3-column layout + Backlinks panel), the full-screen global
// graph (brain's tier/source filter chips + force-directed graph), and the
// People Hub (index + a person page). The bash driver assembles the frames
// into docs/assets/wiki.gif with ffmpeg.
//
// Resolved as CommonJS so `NODE_PATH=<sandbox>/node_modules` finds the
// `playwright` the driver installs into its throwaway sandbox — nothing is
// added to the repo's own node_modules.
//
// Env:
//   WGIF_BASE    base URL of the served wiki (default http://127.0.0.1:8099)
//   WGIF_FRAMES  output directory for the PNG frames (default ./frames)
const { chromium } = require("playwright");
const path = require("path");
const fs = require("fs");

const BASE = (process.env.WGIF_BASE || "http://127.0.0.1:8099").replace(/\/$/, "");
const OUT = process.env.WGIF_FRAMES || path.join(__dirname, "frames");
// Capture at 1600x900 so Quartz renders its full three-column desktop layout
// (left Explorer, center content, right graph/backlinks); the driver scales
// the assembled GIF down to 1200px wide.
const WIDTH = 1600;
const HEIGHT = 900;

fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: WIDTH, height: HEIGHT },
    deviceScaleFactor: 1,
    colorScheme: "light",
  });
  const page = await context.newPage();

  let n = 0;
  const shot = async (label) => {
    const name = String(++n).padStart(2, "0") + "-" + label + ".png";
    await page.screenshot({ path: path.join(OUT, name) });
    process.stdout.write("frame " + name + "\n");
  };
  const goto = async (url) => {
    await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
  };

  // 1) Homepage — brand, Explorer (People + notes), Programs cards, graph panel.
  await goto(BASE + "/");
  await page.waitForSelector(".graph canvas", { timeout: 15000 });
  await sleep(1400);
  await shot("home");

  // 2) A content note — three-column layout, body with wiki-links, the
  //    right-sidebar Backlinks panel + local graph.
  await goto(BASE + "/audit-evidence-portal");
  await page.waitForSelector(".backlinks", { timeout: 15000 });
  await page.waitForSelector(".graph canvas", { timeout: 15000 });
  await sleep(1400);
  await shot("note-backlinks");

  // 3) Full-screen global graph — brain's tier/source filter chips over a
  //    force-directed graph of the whole vault.
  await page.waitForSelector(".global-graph-icon", { timeout: 15000 });
  await page.click(".global-graph-icon");
  await page.waitForSelector(".global-graph-outer.active", { timeout: 15000 });
  await page.waitForSelector(".global-graph-outer.active canvas", { timeout: 15000 });
  await sleep(2600); // let the force simulation settle
  await shot("graph");
  await page.keyboard.press("Escape");
  await sleep(300);

  // 4) People Hub index.
  await goto(BASE + "/people/");
  await sleep(900);
  await shot("people-index");

  // 5) A People Hub person page — meeting roster + the Explorer People group.
  await goto(BASE + "/people/jordan-alvarez");
  await page.waitForSelector(".graph canvas", { timeout: 15000 });
  await sleep(1300);
  await shot("person");

  await browser.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
