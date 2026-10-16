#!/usr/bin/env node
// The logbook in a real browser: a script, not a unit test.
//
// Run: node plugins/logbook/tests/browser_proof.mjs [--browser PATH]
//
// It proves, in a headless Chromium-family browser driven over the DevTools protocol, that the page
// works as a file and not a server: board.html, opened from a file:// URL, redraws in place when
// board.py rewrites state.js, keeping the scroll position and a collapsed section; the colour switch
// in the header offers light, dark and system and remembers the choice; the copy button beside a
// question works or says it could not; text from the state stays text; nothing is requested from any
// network; and report.html, closed and copied away on its own, still opens and never looks for state.js.
// Then, on a board written from fixtures/state-rulings.json: the rulings are drawn by group, with the
// board's two images and no image whose path leaves the board; unticking a ruling and typing a note make
// the answers line; the ticks and the note survive a redraw and a reload; Copy answers copies the line or
// selects it; and the report of that board keeps all of it working, and prints with no answers bar.
//
// Needs Node 22 or later (fetch and WebSocket are built in), python3, and a Chromium-family browser:
// --browser PATH, else $BROWSER, else the usual install places on macOS, Linux and Windows. It builds a
// scratch project and a browser profile in one temporary directory, and removes both, and stops the
// browser it started, when it ends. Prints PASS or FAIL per check; exits 1 if any failed, 2 if no
// browser was found.

import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import zlib from 'node:zlib';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BOARD_CODE = path.join(HERE, '..', 'board');
const TITLE = 'Browser proof of the logbook';
const XSS = '<img src=x onerror="window.__pwned=1">';
const NEW_STEP = 'Step recorded while the page was open';
const SCROLL = 600;

// ---------------------------------------------------------------------------------------------------
// Finding a browser

function browserPath() {
  const args = process.argv.slice(2);
  const at = args.indexOf('--browser');
  const given = at >= 0 ? args[at + 1] : process.env.BROWSER;
  const isFile = (p) => { try { return fs.statSync(p).isFile(); } catch { return false; } };
  // A browser named on the command line or in BROWSER is the only one tried.
  if (given) return isFile(given) ? given : null;
  const candidates = [];
  if (process.platform === 'darwin') {
    const apps = [
      ['Google Chrome', 'Google Chrome'], ['Google Chrome Canary', 'Google Chrome Canary'],
      ['Chromium', 'Chromium'], ['Microsoft Edge', 'Microsoft Edge'], ['Microsoft Edge Beta', 'Microsoft Edge Beta'],
      ['Microsoft Edge Dev', 'Microsoft Edge Dev'], ['Brave Browser', 'Brave Browser'], ['Vivaldi', 'Vivaldi'],
    ];
    for (const dir of ['/Applications', path.join(os.homedir(), 'Applications')]) {
      for (const [app, binary] of apps) candidates.push(path.join(dir, app + '.app', 'Contents', 'MacOS', binary));
    }
  } else if (process.platform === 'win32') {
    const roots = [process.env['ProgramFiles'], process.env['ProgramFiles(x86)'], process.env['LOCALAPPDATA']].filter(Boolean);
    const rels = [
      ['Google', 'Chrome', 'Application', 'chrome.exe'], ['Chromium', 'Application', 'chrome.exe'],
      ['Microsoft', 'Edge', 'Application', 'msedge.exe'], ['BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'],
    ];
    for (const root of roots) for (const rel of rels) candidates.push(path.join(root, ...rel));
  } else {
    const names = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge',
      'microsoft-edge-stable', 'brave-browser'];
    const dirs = (process.env.PATH || '').split(path.delimiter).concat(['/usr/bin', '/usr/local/bin', '/snap/bin']);
    for (const dir of dirs) for (const name of names) candidates.push(path.join(dir, name));
  }
  return candidates.find(isFile) || null;
}

// ---------------------------------------------------------------------------------------------------
// The scratch board, made by board.py itself

const python = 'python3';
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
board.render(folder, now)
print(folder)
`);
  return path.join(scratch.project, '.logbook', 'proof-session');
}

// Rewrite state.js with one more step, the way a running task would.
function recordStep() {
  boardPy(`
folder = board.board_dir(A['project'], 'proof-session')
board.append(folder, 'step', now, id='41', subject=${JSON.stringify(NEW_STEP)})
board.render(folder, now)
`);
}

function closeBoard() {
  boardPy(`board.close(board.board_dir(A['project'], 'proof-session'), now)`);
}

// A small PNG of one colour, so the board has real images to show.
function png(width, height, rgb) {
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type, 'latin1'), data]);
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: width * 3 }, (_, i) => rgb[i % 3]))]);
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', header),
    chunk('IDAT', zlib.deflateSync(Buffer.concat(Array(height).fill(row)))), chunk('IEND', Buffer.alloc(0))]);
}

// The paths a deliverable's image may not take. Each points at a real picture (secret.png beside the
// board folder), so one that slipped through would load and be seen.
const UNSAFE_IMAGES = ['../secret.png', '/etc/secret.png', 'https://example.com/secret.png', ' //example.com/secret.png',
  'images/%2e%2e/../secret.png', 'images\\..\\..\\secret.png', 'javascript:alert(1)'];
const RULING_ADDED = 'A call recorded while the page was open';

// The rulings board: the fixture as the page's state, written as board.py writes board.html, state.js and
// report.html, with the two images it names and the unsafe ones added.
function rulingsState(extra) {
  const state = JSON.parse(fs.readFileSync(path.join(HERE, 'fixtures', 'state-rulings.json'), 'utf-8'));
  state.updated = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  for (const image of UNSAFE_IMAGES) state.deliverables.push({ label: 'Unsafe ' + image, path: null, url: null, step: null, time: null, image });
  if (extra) state.decisions.push({ id: 'D6', text: RULING_ADDED, why: 'To prove a redraw', reverse: 'Nothing', time: null,
    group: null, yours: false, revised: null });
  return state;
}
function writeRulings(folder, state) {
  const file = path.join(scratch.root, 'rulings-state.json');
  fs.writeFileSync(file, JSON.stringify(state));
  boardPy(`
import os
st = json.load(open(${JSON.stringify(file)}, encoding='utf-8'))
folder = ${JSON.stringify(folder)}
template = board.read_text(board.DEFAULT_TEMPLATE)
open(os.path.join(folder, 'board.html'), 'w', encoding='utf-8').write(board.board_page(template))
open(os.path.join(folder, 'state.js'), 'w', encoding='utf-8').write('window.BOARD = ' + board.inline_json(st) + ';\\n')
open(os.path.join(folder, 'report.html'), 'w', encoding='utf-8').write(board.report_page(template, st))
`);
}
function buildRulings() {
  const folder = path.join(scratch.root, 'rulings', 'board');
  fs.mkdirSync(path.join(folder, 'images'), { recursive: true });
  fs.writeFileSync(path.join(folder, 'images', '3-reports-page.png'), png(80, 50, [47, 100, 200]));
  fs.writeFileSync(path.join(folder, 'images', '3-reports-phone.png'), png(40, 90, [27, 106, 51]));
  fs.writeFileSync(path.join(scratch.root, 'rulings', 'secret.png'), png(20, 20, [173, 35, 25]));
  writeRulings(folder, rulingsState(false));
  return folder;
}

// ---------------------------------------------------------------------------------------------------
// The DevTools protocol, over one page's WebSocket

class Page {
  constructor(ws) {
    this.ws = ws;
    this.next = 1;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener('message', (message) => {
      const data = JSON.parse(message.data);
      if (data.id && this.pending.has(data.id)) {
        const { resolve, reject } = this.pending.get(data.id);
        this.pending.delete(data.id);
        if (data.error) reject(new Error(data.error.message)); else resolve(data.result);
      } else if (data.method) {
        for (const listen of this.listeners.slice()) listen(data.method, data.params);
      }
    });
  }
  send(method, params = {}) {
    const id = this.next++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  on(listen) { this.listeners.push(listen); }
  waitFor(method, ms = 20000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.listeners = this.listeners.filter((l) => l !== listen); reject(new Error('timed out waiting for ' + method)); }, ms);
      const listen = (m, params) => {
        if (m !== method) return;
        clearTimeout(timer);
        this.listeners = this.listeners.filter((l) => l !== listen);
        resolve(params);
      };
      this.listeners.push(listen);
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true, userGesture: true });
    if (r.exceptionDetails) throw new Error('page threw: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }
  async open(url) {
    const loaded = this.waitFor('Page.loadEventFired');
    const r = await this.send('Page.navigate', { url });
    if (r.errorText) throw new Error('could not open ' + url + ': ' + r.errorText);
    await loaded;
  }
  async reload() {
    const loaded = this.waitFor('Page.loadEventFired');
    await this.send('Page.reload', {});
    await loaded;
  }
  // A real mouse click on the control with this data-key, after bringing it into view.
  async click(key) {
    const at = await this.eval(`(() => {
      const b = document.querySelector('[data-key="${key}"]');
      if (!b) return null;
      b.scrollIntoView({ block: 'center' });
      const r = b.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()`);
    if (!at) throw new Error('no control ' + key + ' on the page');
    for (const type of ['mousePressed', 'mouseReleased']) {
      await this.send('Input.dispatchMouseEvent', { type, x: at.x, y: at.y, button: 'left', clickCount: 1 });
    }
    await sleep(300);
  }
  async text() { return this.eval('document.body.innerText'); }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(test, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await test()) return true;
    await sleep(500);
  }
  return false;
}

// ---------------------------------------------------------------------------------------------------
// The browser, and everything this run leaves behind

let scratch = null;
let browser = null;

async function startBrowser(executable) {
  const args = [
    '--headless=new', '--remote-debugging-port=0', '--user-data-dir=' + scratch.profile,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--disable-sync',
    '--disable-background-networking', '--disable-component-update', '--window-size=1000,700', 'about:blank',
  ];
  if (process.getuid && process.getuid() === 0) args.push('--no-sandbox');
  browser = spawn(executable, args, { stdio: 'ignore' });
  browser.exited = new Promise((resolve) => browser.once('exit', resolve));
  browser.once('error', () => {});
  const portFile = path.join(scratch.profile, 'DevToolsActivePort');
  let port = null;
  await until(async () => {
    try { port = fs.readFileSync(portFile, 'utf-8').split('\n')[0].trim(); } catch { port = null; }
    return Boolean(port) || browser.exitCode !== null;
  }, 30000);
  if (!port) throw new Error('the browser did not open a DevTools port');
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve); ws.addEventListener('error', reject); });
  return new Page(ws);
}

async function cleanUp() {
  if (browser && browser.exitCode === null && browser.signalCode === null) {
    browser.kill('SIGTERM');
    const done = await Promise.race([browser.exited.then(() => true), sleep(5000).then(() => false)]);
    if (!done) { browser.kill('SIGKILL'); await Promise.race([browser.exited, sleep(5000)]); }
  }
  if (scratch) fs.rmSync(scratch.root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  scratch = null;
}

// ---------------------------------------------------------------------------------------------------
// The checks

// Checks run in the order the page needs (8 watches everything, so it goes last) and print in number order.
const results = [];
function report(number, name, ok, saw) {
  results.push({ number, ok, line: `${ok ? 'PASS' : 'FAIL'} ${number} ${name}: ${saw}` });
}
// A check that throws is a FAIL with what it threw, and the run goes on to the next.
async function check(number, name, body) {
  try {
    const [ok, saw] = await body();
    report(number, name, ok, saw);
  } catch (error) {
    report(number, name, false, 'threw ' + error.message);
  }
}

function brightness(rgb) {
  const [r, g, b] = (rgb.match(/\d+(\.\d+)?/g) || []).map(Number);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}

async function main() {
  const executable = browserPath();
  if (!executable) {
    console.log('No Chromium-family browser found. Pass --browser PATH or set BROWSER.');
    return 2;
  }
  if (spawnSync(python, ['--version']).status !== 0) {
    console.log('python3 is needed to build the board and was not found.');
    return 2;
  }
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'logbook-proof-'));
  scratch = { root, project: path.join(root, 'project'), profile: path.join(root, 'profile') };
  fs.mkdirSync(scratch.project);
  fs.mkdirSync(scratch.profile);
  console.log('browser: ' + executable);

  const folder = buildBoard();
  const boardUrl = pathToFileURL(path.join(folder, 'board.html')).href;
  const page = await startBrowser(executable);

  const requests = [];
  const errors = [];
  page.on((method, params) => {
    if (method === 'Network.requestWillBeSent') requests.push(params.request.url);
    if (method === 'Runtime.exceptionThrown') errors.push(params.exceptionDetails.exception?.description || params.exceptionDetails.text);
  });
  await page.send('Page.enable');
  await page.send('Runtime.enable');
  await page.send('Network.enable');
  await page.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] });
  await page.open(boardUrl);

  await check(1, 'the live board draws', async () => {
    const text = await page.text();
    const ok = text.includes(TITLE) && text.includes('Step number 7 of the proof');
    return [ok, ok ? 'title and step 7 are in the document' : 'document text: ' + JSON.stringify(text.slice(0, 200))];
  });

  await check(7, 'untrusted text stays text', async () => {
    const r = await page.eval(`({ text: document.body.innerText.includes(${JSON.stringify(XSS)}),
      imgs: document.querySelectorAll('#pb-app img').length, pwned: typeof window.__pwned })`);
    const ok = r.text && r.imgs === 0 && r.pwned === 'undefined';
    return [ok, `subject shown as text: ${r.text}, img elements: ${r.imgs}, window.__pwned is ${r.pwned}`];
  });

  // Collapse a section, then scroll to a known place, then have the board rewritten underneath.
  // Checks 3 and 4 mean something only once the page has redrawn, so they fail without it.
  let before = null;
  let refreshed = false;
  await check(2, 'the board refreshes in place', async () => {
    await page.click('toggle-needs');
    before = await page.eval(`(() => {
      window.__proofMarker = 'unchanged';
      window.scrollTo(0, ${SCROLL});
      return { y: window.scrollY, origin: performance.timeOrigin };
    })()`);
    if (Math.abs(before.y - SCROLL) > 1) throw new Error(`the page would only scroll to ${before.y}, not ${SCROLL}`);
    const started = Date.now();
    recordStep();
    const seen = await until(async () => (await page.text()).includes(NEW_STEP), 25000);
    const took = ((Date.now() - started) / 1000).toFixed(1);
    const after = await page.eval(`({ marker: window.__proofMarker, origin: performance.timeOrigin })`);
    const same = after.marker === 'unchanged' && after.origin === before.origin;
    refreshed = seen && same;
    return [seen && same, (seen ? `new step shown after ${took}s` : 'new step not shown within 25s') +
      `, ${same ? 'no navigation (marker and timeOrigin unchanged)' : 'the page navigated'}`];
  });

  await check(3, 'scroll position survives the refresh', async () => {
    if (!refreshed) return [false, 'no refresh in place happened to survive'];
    const y = await page.eval('window.scrollY');
    return [Math.abs(y - before.y) <= 3, `scrollY ${before.y} before, ${y} after`];
  });

  await check(4, 'a collapsed section stays collapsed across the refresh', async () => {
    if (!refreshed) return [false, 'no refresh in place happened to survive'];
    const r = await page.eval(`({ expanded: document.querySelector('[data-key="toggle-needs"]').getAttribute('aria-expanded'),
      hidden: document.getElementById('b-needs').hidden })`);
    return [r.expanded === 'false' && r.hidden === true, `Needs you aria-expanded=${r.expanded}, body hidden=${r.hidden}`];
  });

  await check(5, 'light, dark and system, remembered', async () => {
    const theme = () => page.eval(`document.documentElement.getAttribute('data-theme')`);
    const background = () => page.eval(`getComputedStyle(document.body).backgroundColor`);
    const seen = [];
    await page.click('mode-dark');
    const dark = await theme();
    seen.push('dark chosen: ' + dark);
    await page.reload();
    const remembered = await theme();
    seen.push('after reload: ' + remembered);
    await page.click('mode-system');
    const system = await theme();
    seen.push('system chosen: ' + system);
    await page.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
    await sleep(300);
    const systemBg = await background();
    seen.push('system with dark preferred: ' + systemBg);
    await page.click('mode-light');
    const lightBg = await background();
    seen.push('light with dark preferred: ' + lightBg + ' (' + (await theme()) + ')');
    const ok = dark === 'dark' && remembered === 'dark' && system === 'system' &&
      brightness(systemBg) < 0.3 && brightness(lightBg) > 0.7;
    return [ok, seen.join('; ')];
  });

  await check(6, 'the copy button', async () => {
    try {
      await page.send('Browser.grantPermissions', { permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'] });
    } catch { /* not every browser lets a page session grant it; the page's own fallback is then what is seen */ }
    const thrown = errors.length;
    await page.click('copy-Q1');
    await sleep(700);
    const note = await page.eval(`(document.querySelector('[data-key="copy-Q1"]').parentNode.querySelector('.copied') || {}).textContent || ''`);
    const clip = await page.eval(`navigator.clipboard.readText().then((t) => ({ t }), (e) => ({ e: String(e) }))`);
    const quiet = errors.length === thrown;
    if (clip.t === 'Q1: ') return [quiet, `clipboard holds "Q1: "; the page said "${note}"`];
    const said = note.startsWith('Copied') ? 'the page said it copied' : note.startsWith('Could not copy') ? 'the page said it could not copy' : 'the page said nothing';
    const ok = quiet && note !== '';
    return [ok, `clipboard not readable (${clip.t !== undefined ? JSON.stringify(clip.t) : clip.e}); ${said}: "${note}"` +
      (quiet ? ', nothing thrown' : ', and the page threw: ' + errors.slice(thrown).join(' | '))];
  });

  // The rulings view, on a board written from the rulings fixture.
  const rulings = buildRulings();
  const rulingsView = () => page.eval(`(() => {
    const line = document.getElementById('answers-line');
    const note = document.getElementById('answers-note');
    return {
      groups: [...document.querySelectorAll('.sec-rulings .rgroup')].map((h) => h.textContent),
      ids: [...document.querySelectorAll('[data-ruling]')].map((b) => b.getAttribute('data-ruling')),
      unticked: [...document.querySelectorAll('[data-ruling]')].filter((b) => !b.checked).map((b) => b.getAttribute('data-ruling')),
      reversedRows: [...document.querySelectorAll('.ruling.reversed [data-ruling]')].map((b) => b.getAttribute('data-ruling')),
      marks: [...document.querySelectorAll('.ruling .rmark')].filter((m) => !m.hidden).length,
      yours: [...document.querySelectorAll('.ruling')].filter((r) => r.querySelector('.yours')).map((r) => r.querySelector('[data-ruling]').getAttribute('data-ruling')),
      line: line ? line.textContent : null,
      note: note ? note.value : null,
      focused: document.activeElement && document.activeElement.getAttribute('data-key'),
      text: document.body.innerText,
    };
  })()`);
  const head = 'Logbook "CSV export for the reports page" 5 Jan: ';
  const NOTE = 'Make the icon bigger';

  await check(10, 'the rulings are drawn by group, with the lede, the facts and the images', async () => {
    await page.open(pathToFileURL(path.join(rulings, 'board.html')).href);
    const v = await rulingsView();
    const imgs = await page.eval(`[...document.querySelectorAll('#pb-app img')].map((i) => [i.getAttribute('src'), i.complete && i.naturalWidth > 0])`);
    const ok = JSON.stringify(v.groups) === '["Data format","Page layout","Other"]' &&
      JSON.stringify(v.ids) === '["D1","D3","D2","D5","D4","Q3"]' && v.unticked.length === 0 &&
      JSON.stringify(v.yours) === '["D2","Q3"]' && v.line === head + 'keep all' &&
      v.text.includes('Download CSV button in its header') && v.text.includes('4 files changed') &&
      JSON.stringify(imgs) === '[["images/3-reports-page.png",true],["images/3-reports-phone.png",true]]';
    return [ok, `groups ${JSON.stringify(v.groups)}, rows ${JSON.stringify(v.ids)}, unticked ${JSON.stringify(v.unticked)}, ` +
      `yours ${JSON.stringify(v.yours)}, line "${v.line}", images ${JSON.stringify(imgs)}`];
  });

  await check(11, 'an image path that leaves the board is not rendered', async () => {
    const r = await page.eval(`({ imgs: document.querySelectorAll('#pb-app img').length,
      unsafeShown: document.body.innerText.includes('Unsafe ../secret.png') })`);
    const asked = requests.filter((u) => u.includes('secret') || u.includes('example.com'));
    const ok = r.imgs === 2 && asked.length === 0 && r.unsafeShown;
    return [ok, `${r.imgs} img elements, requests for the unsafe paths: ${asked.length ? asked.join(', ') : 'none'}, ` +
      `the unsafe deliverables listed as text in Built: ${r.unsafeShown}`];
  });

  await check(12, 'unticking D2 and typing a note make the answers line', async () => {
    await page.click('tick-D2');
    const after = await rulingsView();
    await page.click('answers-note');
    await page.send('Input.insertText', { text: NOTE });
    await sleep(300);
    const typed = await rulingsView();
    const ok = after.line === head + 'reverse D2' && JSON.stringify(after.reversedRows) === '["D2"]' && after.marks === 1 &&
      typed.line === head + 'reverse D2 | note: ' + NOTE;
    return [ok, `after the untick: "${after.line}", rows marked to reverse ${JSON.stringify(after.reversedRows)}; ` +
      `after typing: "${typed.line}"`];
  });

  await check(13, 'the ticks and the note survive a redraw and a reload', async () => {
    const marker = await page.eval(`(window.__proofMarker = 'unchanged', performance.timeOrigin)`);
    writeRulings(rulings, rulingsState(true));
    const seen = await until(async () => (await page.text()).includes(RULING_ADDED), 25000);
    const redrawn = await rulingsView();
    const same = await page.eval(`window.__proofMarker === 'unchanged' && performance.timeOrigin === ${marker}`);
    await page.reload();
    const reloaded = await rulingsView();
    const want = head + 'reverse D2 | note: ' + NOTE;
    const ok = seen && same && JSON.stringify(redrawn.unticked) === '["D2"]' && redrawn.note === NOTE &&
      redrawn.line === want && redrawn.focused === 'answers-note' &&
      JSON.stringify(reloaded.unticked) === '["D2"]' && reloaded.note === NOTE && reloaded.line === want;
    return [ok, (seen && same ? 'redrawn in place' : seen ? 'the page navigated' : 'no redraw within 25s') +
      `: unticked ${JSON.stringify(redrawn.unticked)}, note ${JSON.stringify(redrawn.note)}, focus on ${redrawn.focused}, ` +
      `line "${redrawn.line}"; after a reload: unticked ${JSON.stringify(reloaded.unticked)}, note ${JSON.stringify(reloaded.note)}`];
  });

  const copied = async () => {
    const thrown = errors.length;
    await page.click('copy-answers');
    await sleep(700);
    const r = await page.eval(`({ said: document.querySelector('.answerbar .copied').textContent,
      line: document.getElementById('answers-line').textContent, selected: String(window.getSelection()) })`);
    const clip = await page.eval(`navigator.clipboard.readText().then((t) => ({ t }), (e) => ({ e: String(e) }))`);
    const quiet = errors.length === thrown;
    if (clip.t === r.line) return [quiet && r.said.startsWith('Copied'), `clipboard holds the line; the page said "${r.said}"`, r.line];
    const ok = quiet && r.said.startsWith('Selected. Copy it with') && r.selected === r.line;
    return [ok, `clipboard not readable or not the line (${clip.t !== undefined ? JSON.stringify(clip.t) : clip.e}); ` +
      `the page said "${r.said}" and selected ${r.selected === r.line ? 'the line' : JSON.stringify(r.selected)}`, r.line];
  };

  await check(14, 'Copy answers copies the line, or selects it', async () => {
    const [ok, saw, line] = await copied();
    return [ok && line === head + 'reverse D2 | note: ' + NOTE, saw + ` (line "${line}")`];
  });

  await check(15, 'the report keeps the rulings working, and prints every row without the bar', async () => {
    await page.open(pathToFileURL(path.join(rulings, 'report.html')).href);
    const first = await rulingsView();
    await page.click('tick-D1');
    const after = await rulingsView();
    const [copiedOk, saw] = await copied();
    await page.send('Emulation.setEmulatedMedia', { media: 'print' });
    const printed = await page.eval(`({ bar: getComputedStyle(document.querySelector('.answerbar')).display,
      rows: [...document.querySelectorAll('.ruling')].filter((r) => r.getBoundingClientRect().height > 0).length,
      boxes: [...document.querySelectorAll('[data-ruling]')].map((b) => b.checked ? 1 : 0).join('') })`);
    await page.send('Emulation.setEmulatedMedia', { media: '' });
    const ok = JSON.stringify(first.unticked) === '["D2"]' && after.line === head + 'reverse D1, D2 | note: ' + NOTE &&
      copiedOk && printed.bar === 'none' && printed.rows === 7 && printed.boxes === '0101111';
    return [ok, `opened with ${JSON.stringify(first.unticked)} unticked; after unticking D1: "${after.line}"; copy: ${saw}; ` +
      `in print the bar is display:${printed.bar}, ${printed.rows} rows drawn, ticks ${printed.boxes}`];
  });

  // The report is closed, then copied on its own into a folder with nothing beside it.
  let reportRequests = 0;
  await check(9, 'report.html stands alone', async () => {
    closeBoard();
    const alone = path.join(scratch.root, 'report-alone');
    fs.mkdirSync(alone);
    fs.copyFileSync(path.join(folder, 'report.html'), path.join(alone, 'report.html'));
    const from = requests.length;
    await page.open(pathToFileURL(path.join(alone, 'report.html')).href);
    const text = await page.text();
    const switches = await page.eval(`['light', 'dark', 'system'].filter((m) => document.querySelector('[data-key="mode-' + m + '"]')).length`);
    let tags = 0;
    for (let i = 0; i < 15; i++) {
      tags = Math.max(tags, await page.eval(`document.querySelectorAll('script[src*="state.js"]').length`));
      await sleep(1000);
    }
    const asked = requests.slice(from).filter((u) => u.includes('state.js')).length;
    reportRequests = requests.length - from;
    const ok = text.includes(TITLE) && switches === 3 && tags === 0 && asked === 0;
    return [ok, `title shown: ${text.includes(TITLE)}, theme switch buttons: ${switches}, ` +
      `state.js script tags over 15s: ${tags}, requests for state.js: ${asked}`];
  });

  await check(8, 'no network', async () => {
    const outside = requests.filter((u) => !/^(file|data|about):/.test(u));
    const ok = outside.length === 0 && requests.some((u) => u.startsWith('file:'));
    return [ok, outside.length ? 'requests off the machine: ' + outside.join(', ')
      : `${requests.length} requests, all file://, data: or about: (${reportRequests} of them from the report)`];
  });

  return results.every((r) => r.ok) && results.length === 15 ? 0 : 1;
}

let code = 1;
process.on('SIGINT', () => { cleanUp().finally(() => process.exit(130)); });
try {
  code = await main();
} catch (error) {
  console.log('FAIL the run stopped: ' + error.message);
  code = 1;
} finally {
  for (const r of results.sort((a, b) => a.number - b.number)) console.log(r.line);
  await cleanUp();
}
process.exit(code);
