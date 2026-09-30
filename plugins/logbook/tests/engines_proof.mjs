#!/usr/bin/env node
// The logbook in the three browser engines: a script, not a unit test.
//
// Run: node plugins/logbook/tests/engines_proof.mjs [--engine webkit|firefox|chromium]... [--playwright DIR]
//
// The same proof as browser_proof.mjs, driven through Playwright so that it reaches WebKit (Safari's
// engine) and Gecko (Firefox's) as well as Chromium: board.html, opened from a file:// URL, redraws in
// place when board.py rewrites state.js, keeping the scroll position, a collapsed section, and the
// Commands section once opened (it starts collapsed); the colour switch offers light, dark and system,
// and remembers the choice where the engine gives a file:// page storage; the copy button beside a
// question works or says it could not; text from the state stays text; nothing is requested from any
// network; and report.html, closed and copied away on its own, still opens and never looks for state.js.
//
// Playwright is not a dependency of this repository. The script takes it from --playwright DIR (a folder
// with node_modules/playwright below it, or that node_modules itself), else from wherever Node resolves
// it, NODE_PATH included. To get it and its browsers without installing anything globally:
//   npm install --prefix DIR playwright
//   PLAYWRIGHT_BROWSERS_PATH=DIR/browsers npx --prefix DIR playwright install webkit firefox chromium
// and run with the same PLAYWRIGHT_BROWSERS_PATH. Needs python3 too. Each engine gets a scratch project
// and a browser profile inside one temporary directory, which is removed, and every browser it opened is
// closed, when it ends. Prints PASS or FAIL per check per engine; exits 1 if any failed, 2 if Playwright
// or python3 was not found.

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BOARD_CODE = path.join(HERE, '..', 'board');
const TITLE = 'Browser proof of the logbook';
const XSS = '<img src=x onerror="window.__pwned=1">';
const NEW_STEP = 'Step recorded while the page was open';
const LATER_STEP = 'Step recorded with the commands open';
const FIRST_COMMAND = 'ls -la proof-first-command';
const LATER_COMMAND = 'ls -la proof-later-command';
const SCROLL = 600;
const ENGINES = ['webkit', 'firefox', 'chromium'];

// ---------------------------------------------------------------------------------------------------
// The command line, and Playwright

function options() {
  const args = process.argv.slice(2);
  const engines = [];
  let from = null;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--engine') engines.push(args[++i]);
    else if (args[i] === '--playwright') from = args[++i];
    else throw new Error('unknown argument ' + args[i]);
  }
  for (const e of engines) if (!ENGINES.includes(e)) throw new Error('unknown engine ' + e + '; one of ' + ENGINES.join(', '));
  return { engines: engines.length ? engines : ENGINES, from };
}

// CommonJS resolution, so NODE_PATH counts; an import statement would ignore it.
function loadPlaywright(from) {
  const require = createRequire(import.meta.url);
  try {
    const at = from ? require.resolve('playwright', { paths: [path.resolve(from)] }) : require.resolve('playwright');
    return require(at);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------------------------------
// The scratch board, made by board.py itself

const python = 'python3';
let scratch = null;

function boardPy(code) {
  // Git variables from a surrounding hook would point board.py's git reads at another repository.
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith('GIT_')));
  const prelude = 'import sys; sys.path.insert(0, sys.argv[1]); import board, json; ' +
    'from datetime import datetime, timezone; now = datetime.now(timezone.utc); A = json.loads(sys.argv[2])\n';
  const run = spawnSync(python, ['-c', prelude + code, BOARD_CODE, JSON.stringify(scratch)], { env, encoding: 'utf-8' });
  if (run.status !== 0) throw new Error('board.py failed: ' + (run.stderr || run.error));
  return run.stdout.trim();
}

function buildBoard() {
  boardPy(`
folder = board.start(A['project'], 'proof-session', now, ${JSON.stringify(TITLE)})
for i in range(1, 41):
    board.append(folder, 'step', now, id=str(i), subject='Step number %d of the proof' % i)
    board.append(folder, 'step-status', now, id=str(i), status='completed' if i < 20 else 'in_progress' if i == 20 else 'pending')
board.append(folder, 'step', now, id='xss', subject=${JSON.stringify(XSS)})
for i in range(1, 13):
    board.append(folder, 'decision', now, text='Decision number %d' % i, why='Because of reason %d' % i, reverse='Undo %d' % i)
board.append(folder, 'question', now, text='Which way should this go?', default='The first way', affects='The layout', reverse='Switch it back', hardStop=False)
for i in range(1, 4):
    board.append(folder, 'check', now, proves='Check number %d holds' % i, command='true', result='pass', source='model', agent=None)
board.append(folder, 'command', now, command=${JSON.stringify(FIRST_COMMAND)}, result='pass', use='proof-use-1')
board.render(folder, now)
print(folder)
`);
  return path.join(scratch.project, '.logbook', 'proof-session');
}

// Rewrite state.js with one more step (and, for the Commands check, one more command), the way a
// running task would.
function recordStep(subject, command) {
  boardPy(`
folder = board.board_dir(A['project'], 'proof-session')
board.append(folder, 'step', now, id=${JSON.stringify(subject)}, subject=${JSON.stringify(subject)})
${command ? `board.append(folder, 'command', now, command=${JSON.stringify(command)}, result='pass', use='proof-use-2')` : ''}
board.render(folder, now)
`);
}

function closeBoard() {
  boardPy(`board.close(board.board_dir(A['project'], 'proof-session'), now)`);
}

// ---------------------------------------------------------------------------------------------------
// The page

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(test, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await test()) return true;
    await sleep(500);
  }
  return false;
}

// A real mouse click on the control with this data-key, after bringing it into view the way
// browser_proof.mjs does, so both proofs scroll the same.
async function click(page, key) {
  const at = await page.evaluate((k) => {
    const b = document.querySelector('[data-key="' + k + '"]');
    if (!b) return null;
    b.scrollIntoView({ block: 'center' });
    const r = b.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }, key);
  if (!at) throw new Error('no control ' + key + ' on the page');
  await page.mouse.click(at.x, at.y);
  await sleep(300);
}

const text = (page) => page.evaluate(() => document.body.innerText);

// Two refresh periods, read from the page itself, which is how long a new event may take to show.
async function twoPeriods(page) {
  const seconds = await page.evaluate(() => window.PB.refreshSeconds((window.BOARD || {}).settings));
  return 2 * seconds * 1000;
}

function brightness(rgb) {
  const [r, g, b] = (rgb.match(/\d+(\.\d+)?/g) || []).map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}

// ---------------------------------------------------------------------------------------------------
// The checks, for one engine

// Checks run in the order the page needs (9 watches everything, so it goes last) and print in number order.
let results = [];
function report(number, name, ok, saw) {
  results.push({ number, ok, line: `${ok ? 'PASS' : 'FAIL'} ${number} ${name}: ${saw}` });
}
// A check that throws is a FAIL with what it threw, and the run goes on to the next.
async function check(number, name, body) {
  try {
    const [ok, saw] = await body();
    report(number, name, ok, saw);
  } catch (error) {
    report(number, name, false, 'threw ' + error.message.split('\n')[0]);
  }
}

let context = null;

async function proveEngine(playwright, engine) {
  const root = path.join(scratch.root, engine);
  scratch = { ...scratch, project: path.join(root, 'project'), profile: path.join(root, 'profile') };
  fs.mkdirSync(scratch.project, { recursive: true });
  fs.mkdirSync(scratch.profile, { recursive: true });
  const folder = buildBoard();
  const boardUrl = pathToFileURL(path.join(folder, 'board.html')).href;

  // A persistent profile, as a person's browser has, rather than a throwaway in-memory one.
  context = await playwright[engine].launchPersistentContext(scratch.profile, {
    headless: true, viewport: { width: 1000, height: 700 }, colorScheme: 'light',
  });
  const version = context.browser() ? context.browser().version() : 'version not reported';
  console.log(`engine: ${engine} ${version}`);
  const page = context.pages()[0] || await context.newPage();

  const requests = [];
  const errors = [];
  const consoled = [];
  page.on('request', (r) => requests.push(r.url()));
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') consoled.push(m.text()); });
  await page.goto(boardUrl);

  await check(1, 'the live board draws', async () => {
    const t = await text(page);
    const ok = t.includes(TITLE) && t.includes('Step number 7 of the proof');
    return [ok, ok ? 'title and step 7 are in the document' : 'document text: ' + JSON.stringify(t.slice(0, 200))];
  });

  await check(8, 'untrusted text stays text', async () => {
    const r = await page.evaluate((xss) => ({ text: document.body.innerText.includes(xss),
      imgs: document.querySelectorAll('#pb-app img').length, pwned: typeof window.__pwned }), XSS);
    const ok = r.text && r.imgs === 0 && r.pwned === 'undefined';
    return [ok, `subject shown as text: ${r.text}, img elements: ${r.imgs}, window.__pwned is ${r.pwned}`];
  });

  // Collapse a section, then scroll to a known place, then have the board rewritten underneath.
  // Checks 3 and 4 mean something only once the page has redrawn, so they fail without it.
  let before = null;
  let refreshed = false;
  await check(2, 'the board refreshes in place', async () => {
    await click(page, 'toggle-decisions');
    before = await page.evaluate((y) => {
      window.__proofMarker = 'unchanged';
      window.scrollTo(0, y);
      return { y: window.scrollY, origin: performance.timeOrigin };
    }, SCROLL);
    if (Math.abs(before.y - SCROLL) > 1) throw new Error(`the page would only scroll to ${before.y}, not ${SCROLL}`);
    const limit = await twoPeriods(page);
    const started = Date.now();
    recordStep(NEW_STEP, null);
    const seen = await until(async () => (await text(page)).includes(NEW_STEP), limit);
    const took = ((Date.now() - started) / 1000).toFixed(1);
    const after = await page.evaluate(() => ({ marker: window.__proofMarker, origin: performance.timeOrigin }));
    const same = after.marker === 'unchanged' && after.origin === before.origin;
    refreshed = seen && same;
    return [seen && same, (seen ? `new step shown after ${took}s` : `new step not shown within ${limit / 1000}s`) +
      `, ${same ? 'no navigation (marker and timeOrigin unchanged)' : 'the page navigated'}`];
  });

  await check(3, 'scroll position survives the refresh', async () => {
    if (!refreshed) return [false, 'no refresh in place happened to survive'];
    const y = await page.evaluate(() => window.scrollY);
    return [Math.abs(y - before.y) <= 3, `scrollY ${before.y} before, ${y} after`];
  });

  await check(4, 'a collapsed section stays collapsed across the refresh', async () => {
    if (!refreshed) return [false, 'no refresh in place happened to survive'];
    const r = await page.evaluate(() => ({ expanded: document.querySelector('[data-key="toggle-decisions"]').getAttribute('aria-expanded'),
      hidden: document.getElementById('b-decisions').hidden }));
    return [r.expanded === 'false' && r.hidden === true, `Decisions aria-expanded=${r.expanded}, body hidden=${r.hidden}`];
  });

  // The Commands tab is not the one showing at first; a viewer who picks it keeps it showing.
  await check(5, 'the Commands tab is hidden until picked and stays picked', async () => {
    const state = () => page.evaluate(() => {
      const b = document.querySelector('[data-key="tab-commands"]');
      const body = document.getElementById('b-commands');
      return b && body ? { expanded: b.getAttribute('aria-selected'), hidden: body.hidden, text: body.innerText } : null;
    });
    const first = await state();
    if (!first) return [false, 'no Commands section on the page'];
    await click(page, 'tab-commands');
    const opened = await state();
    const limit = await twoPeriods(page);
    recordStep(LATER_STEP, LATER_COMMAND);
    const seen = await until(async () => (await text(page)).includes(LATER_STEP), limit);
    const after = await state();
    const ok = first.expanded === 'false' && first.hidden === true && opened.expanded === 'true' && opened.hidden === false &&
      opened.text.includes(FIRST_COMMAND) && seen && after.expanded === 'true' && after.hidden === false &&
      after.text.includes(LATER_COMMAND);
    return [ok, `at first aria-expanded=${first.expanded} hidden=${first.hidden}; clicked: aria-expanded=${opened.expanded} ` +
      `hidden=${opened.hidden}, first command shown: ${opened.text.includes(FIRST_COMMAND)}; ` +
      (seen ? 'after the refresh' : `no refresh within ${limit / 1000}s, then`) +
      `: aria-expanded=${after.expanded} hidden=${after.hidden}, new command shown: ${after.text.includes(LATER_COMMAND)}`];
  });

  // An engine may give a file:// page no storage, or storage that does not outlive the document. The
  // probe written beside the choice tells which: a probe that survived the reload with the choice lost
  // is the page's fault; a probe that did not is the engine's, and the switch must still work.
  await check(6, 'light, dark and system', async () => {
    const theme = () => page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const probe = (write) => page.evaluate((w) => {
      try {
        if (w) window.localStorage.setItem('logbook-proof-probe', 'kept');
        return String(window.localStorage.getItem('logbook-proof-probe'));
      } catch (e) { return 'refused (' + e.name + ')'; }
    }, write);
    const seen = [];
    await click(page, 'mode-dark');
    const dark = await theme();
    seen.push('dark chosen: ' + dark);
    const probeBefore = await probe(true);
    await page.reload();
    const remembered = await theme();
    const probeAfter = await probe(false);
    const kept = probeAfter === 'kept';
    seen.push(`after reload: ${remembered} (storage probe ${probeBefore} before, ${probeAfter} after)`);
    await click(page, 'mode-system');
    const system = await theme();
    seen.push('system chosen: ' + system);
    await page.emulateMedia({ colorScheme: 'dark' });
    await sleep(300);
    const systemBg = await background();
    seen.push('system with dark preferred: ' + systemBg);
    await click(page, 'mode-light');
    const lightBg = await background();
    seen.push('light with dark preferred: ' + lightBg + ' (' + (await theme()) + ')');
    const switches = dark === 'dark' && system === 'system' && brightness(systemBg) < 0.3 && brightness(lightBg) > 0.7;
    const memory = remembered === 'dark' ? 'remembered across the reload'
      : kept ? 'NOT remembered, though storage kept a value across the reload'
      : 'not remembered: the engine keeps no storage for this file:// page, so the switch works until the page closes';
    return [switches && (remembered === 'dark' || !kept), memory + '; ' + seen.join('; ')];
  });

  await check(7, 'the copy button', async () => {
    // Not every engine knows both permissions, and one it refuses stays on the context and breaks every
    // page opened after it, so each is tried alone and only those accepted are kept. Without them the
    // page's own fallback is what is seen.
    const granted = [];
    for (const permission of ['clipboard-read', 'clipboard-write']) {
      try { await context.grantPermissions([...granted, permission]); granted.push(permission); } catch { /* refused */ }
      await context.clearPermissions();
    }
    if (granted.length) await context.grantPermissions(granted);
    const thrown = errors.length;
    await click(page, 'copy-Q1');
    await sleep(700);
    const note = await page.evaluate(() => (document.querySelector('[data-key="copy-Q1"]').parentNode.querySelector('.copied') || {}).textContent || '');
    // A read the engine turns into a prompt for a person never settles, so it is given two seconds.
    const clip = await page.evaluate(() => Promise.race([
      navigator.clipboard.readText().then((t) => ({ t }), (e) => ({ e: String(e) })),
      new Promise((resolve) => setTimeout(() => resolve({ e: 'no answer within 2s' }), 2000)),
    ]));
    const quiet = errors.length === thrown;
    if (clip.t === 'Q1: ') return [quiet, `clipboard holds "Q1: "; the page said "${note}"`];
    const said = note.startsWith('Copied') ? 'the page said it copied' : note.startsWith('Could not copy') ? 'the page said it could not copy' : 'the page said nothing';
    const ok = quiet && note !== '';
    return [ok, `clipboard not readable (${clip.t !== undefined ? JSON.stringify(clip.t) : clip.e}); ${said}: "${note}"` +
      (quiet ? ', nothing thrown' : ', and the page threw: ' + errors.slice(thrown).join(' | '))];
  });

  // The report is closed, then copied on its own into a folder with nothing beside it.
  let reportRequests = 0;
  await check(10, 'report.html stands alone', async () => {
    closeBoard();
    const alone = path.join(root, 'report-alone');
    fs.mkdirSync(alone);
    fs.copyFileSync(path.join(folder, 'report.html'), path.join(alone, 'report.html'));
    const from = requests.length;
    await page.goto(pathToFileURL(path.join(alone, 'report.html')).href);
    const t = await text(page);
    const switches = await page.evaluate(() => ['light', 'dark', 'system'].filter((m) => document.querySelector('[data-key="mode-' + m + '"]')).length);
    let tags = 0;
    for (let i = 0; i < 15; i++) {
      tags = Math.max(tags, await page.evaluate(() => document.querySelectorAll('script[src*="state.js"]').length));
      await sleep(1000);
    }
    const asked = requests.slice(from).filter((u) => u.includes('state.js')).length;
    reportRequests = requests.length - from;
    const ok = t.includes(TITLE) && switches === 3 && tags === 0 && asked === 0;
    return [ok, `title shown: ${t.includes(TITLE)}, theme switch buttons: ${switches}, ` +
      `state.js script tags over 15s: ${tags}, requests for state.js: ${asked}`];
  });

  // The request log is worth something only if it would have seen a request to a network, so a control
  // page in the same browser asks a server of this script's own, on the loopback address, for an image,
  // and the log must see it. An engine whose driver reports no file:// load (Firefox) leaves the control
  // as the only evidence that the log works; the line says so.
  await check(9, 'no network', async () => {
    const server = http.createServer((_, res) => { res.writeHead(204); res.end(); });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const control = await context.newPage();
    let controlSeen = null;
    try {
      const asked = control.waitForEvent('request', { timeout: 5000 }).then((r) => r.url(), () => null);
      await control.setContent(`<img src="http://127.0.0.1:${server.address().port}/proof-control.png">`);
      controlSeen = await asked;
    } finally {
      await control.close();
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
    const outside = requests.filter((u) => !/^(file|data|about):/.test(u));
    const files = requests.some((u) => u.startsWith('file:'));
    const ok = outside.length === 0 && controlSeen !== null;
    return [ok, (outside.length ? 'requests off the machine: ' + outside.join(', ')
      : `${requests.length} requests, all file://, data: or about: (${reportRequests} of them from the report)`) +
      (files ? '' : ', the engine\'s driver reporting no file:// load') +
      `; a control request to a loopback server was ${controlSeen ? 'seen' : 'NOT seen, so the log proves nothing'}`];
  });

  if (errors.length) console.log('page errors: ' + errors.join(' | '));
  if (consoled.length) console.log('console errors and warnings: ' + consoled.join(' | '));
}

// ---------------------------------------------------------------------------------------------------
// Every browser opened, and everything this run leaves behind

async function closeBrowser() {
  const open = context;
  context = null;
  if (open) await Promise.race([open.close().catch(() => {}), sleep(10000)]);
}

async function cleanUp() {
  await closeBrowser();
  if (scratch) fs.rmSync(scratch.root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  scratch = null;
}

async function main() {
  const { engines, from } = options();
  const playwright = loadPlaywright(from);
  if (!playwright) {
    console.log('Playwright was not found' + (from ? ' under ' + from : '') + '. It is not a dependency of this ' +
      'repository; to run this proof, install it and its browsers in a folder of your choosing:\n' +
      '  npm install --prefix DIR playwright\n' +
      '  PLAYWRIGHT_BROWSERS_PATH=DIR/browsers npx --prefix DIR playwright install webkit firefox chromium\n' +
      'then run, with the same PLAYWRIGHT_BROWSERS_PATH:\n' +
      '  node plugins/logbook/tests/engines_proof.mjs --playwright DIR');
    return 2;
  }
  if (spawnSync(python, ['--version']).status !== 0) {
    console.log('python3 is needed to build the board and was not found.');
    return 2;
  }
  scratch = { root: fs.mkdtempSync(path.join(os.tmpdir(), 'logbook-engines-')) };
  const version = JSON.parse(fs.readFileSync(path.join(path.dirname(
    createRequire(import.meta.url).resolve('playwright', from ? { paths: [path.resolve(from)] } : undefined)), 'package.json'), 'utf-8')).version;
  console.log('playwright: ' + version);

  const summary = [];
  for (const engine of engines) {
    results = [];
    try {
      await proveEngine(playwright, engine);
    } catch (error) {
      console.log(`FAIL the run in ${engine} stopped: ` + error.message.split('\n')[0]);
      results.push({ number: 0, ok: false, line: null });
    } finally {
      await closeBrowser();
    }
    for (const r of results.filter((r) => r.line).sort((a, b) => a.number - b.number)) console.log(r.line);
    const passed = results.filter((r) => r.ok).length;
    const whole = passed === 10 && results.length === 10;
    summary.push({ engine, whole, line: `${engine}: ${passed} of 10 checks passed` });
    console.log('');
  }
  for (const s of summary) console.log(s.line);
  return summary.every((s) => s.whole) ? 0 : 1;
}

let code = 1;
process.on('SIGINT', () => { cleanUp().finally(() => process.exit(130)); });
try {
  code = await main();
} catch (error) {
  console.log('FAIL the run stopped: ' + error.message);
  code = 1;
} finally {
  await cleanUp();
}
process.exit(code);
