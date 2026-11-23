'use strict';
// Supply Playwright through NODE_PATH and Chromium through CHROME_PATH.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');

const origin = 'https://reading-fixture.test';
const pageUrl = origin + '/items/4';
const source = 'https://arxiv.org/html/2611.00001v2';
const fixtureFile = process.argv[2];
if (!fixtureFile) throw new Error('Pass HTML rendered by checks.test_v11_room.render_fixture()');
const visual = {
  state: 'ready', source, truncated: false,
  panels: [{kind:'table', anchor:'S2.T3', caption:'Table 3: Fixture measurements.', images:[],
    steps:[], references:[], rows:[['Method','Latency'],['Trial','71 ms']].map(row => row.map(text => ({
      text, header:false, colspan:1, rowspan:1
    })))}]
};

async function fixture(browser, width) {
  const html = await fs.readFile(fixtureFile, 'utf8');
  const context = await browser.newContext({viewport:{width, height:860}});
  const page = await context.newPage();
  const errors = [];
  const saves = [];
  let releaseVisual;
  const visualGate = new Promise(resolve => {releaseVisual = resolve;});
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    const requestPath = url.pathname;
    if (requestPath === '/items/4') return route.fulfill({contentType:'text/html', body:html});
    if (requestPath === '/items/4/visuals') {
      assert.equal(url.searchParams.get('version'), '7');
      await visualGate;
      return route.fulfill({json:visual});
    }
    if (requestPath === '/items/4/reading') {
      saves.push(Object.fromEntries(new URLSearchParams(route.request().postData())));
      return route.fulfill({json:{revision:3 + saves.length}});
    }
    if (requestPath === '/static/layers.js' || requestPath === '/static/reading-actions.css') return route.fulfill({status:204});
    if (requestPath.startsWith('/static/')) {
      const body = await fs.readFile(path.join('knowledge_hub', requestPath));
      return route.fulfill({body, contentType:requestPath.endsWith('.css') ? 'text/css' : 'application/javascript'});
    }
    return route.fulfill({status:404});
  });
  await page.goto(pageUrl);
  await page.locator('#layer-guide').evaluate(node => {
    const quote = document.createElement('a');
    quote.id = 'fixture-quote';
    quote.href = '/items/4?version=7#S2.p10';
    quote.textContent = '段落證據';
    const figure = document.createElement('a');
    figure.id = 'fixture-table';
    figure.href = '/items/4?version=7#kh-visual-S2.T3';
    figure.textContent = '圖表證據';
    node.prepend(quote, figure);
  });
  return {context, page, errors, saves, releaseVisual};
}

async function reveal(page, anchor, quote, version = '7') {
  return page.locator('[data-reading-room]').evaluate((node, values) => !node.dispatchEvent(
    new CustomEvent('kh-reveal-evidence', {cancelable:true, detail:values})
  ), {anchor, quote, href:'/items/4?version=' + version + '#' + anchor});
}

async function desktop(browser) {
  const {context, page, errors, saves, releaseVisual} = await fixture(browser, 1360);
  try {
    const left = page.locator('#panel-lesson');
    const right = page.locator('#panel-original');
    assert.equal(await left.isVisible(), true);
    assert.equal(await right.isVisible(), true);
    assert.ok((await left.boundingBox()).x < (await right.boundingBox()).x);
    assert.equal(await reveal(page, 'S2.p10', 'The measured source text.', '8'), false);
    assert.equal(await reveal(page, 'S2.p10', 'The measured source text.'), true);
    await page.waitForFunction(() => document.activeElement?.id === 'S2.p10');
    assert.equal(await page.locator('.room-highlight').getAttribute('id'), 'S2.p10');
    assert.equal(await page.evaluate(() => CSS.highlights.get('room-evidence').size), 1);
    assert.ok(await right.evaluate(pane => pane.scrollTop > 100));
    assert.equal(await left.evaluate(pane => pane.scrollTop), 0);
    assert.equal(await page.locator('#panel-original a[href="/items/4/pdf?version=7"]').count(), 1);
    assert.equal(await page.locator('#fixture-quote').count(), 1);
    assert.equal(new URL(page.url()).searchParams.get('version'), '7');

    assert.equal(await reveal(page, 'kh-visual-S2.T3', 'Trial | 71 ms'), true);
    releaseVisual();
    const card = page.locator('[id="kh-visual-S2.T3"]');
    await card.waitFor();
    assert.equal(await card.evaluate(node => node.open), true);
    assert.equal(await card.locator('.room-row-highlight').count(), 1);
    await page.waitForFunction(() => document.activeElement?.id === 'kh-visual-S2.T3');
    await page.waitForFunction(() => location.hash === '#kh-visual-S2.T3');
    for (let attempt = 0; attempt < 20 && !saves.some(row => row.anchor === 'kh-visual-S2.T3'); attempt++)
      await new Promise(resolve => setTimeout(resolve, 75));
    assert.ok(saves.some(row => row.version_id === '7' && row.tab === 'original' && row.anchor === 'kh-visual-S2.T3'));

    const focus = right.locator('[data-room-focus]');
    await focus.click();
    assert.equal(await focus.getAttribute('aria-pressed'), 'true');
    assert.equal(await left.isVisible(), false);
    await focus.click();
    assert.equal(await left.isVisible(), true);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
}

async function mobile(browser, width) {
  const {context, page, errors, releaseVisual} = await fixture(browser, width);
  try {
    const left = page.locator('#room-guide-tab'), right = page.locator('#room-source-tab');
    assert.equal(await right.getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#panel-lesson').isVisible(), false);
    await right.focus();
    await right.press('Home');
    assert.equal(await left.getAttribute('aria-selected'), 'true');
    assert.equal(await left.evaluate(element => element === document.activeElement), true);
    await page.locator('#fixture-quote').click();
    assert.equal(await right.getAttribute('aria-selected'), 'true');
    await page.waitForFunction(() => document.activeElement?.id === 'S2.p10');
    const drawer = page.locator('[data-room-drawer]');
    const ask = page.locator('[data-room-ask]');
    await ask.click();
    assert.equal(await drawer.isVisible(), true);
    assert.equal(await page.locator('#followup').inputValue(), 'Return to the draft.');
    assert.equal(await page.locator('#followup').evaluate(element => element === document.activeElement), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
    await page.keyboard.press('Escape');
    assert.equal(await drawer.isVisible(), false);
    assert.equal(await ask.evaluate(element => element === document.activeElement), true);
    assert.deepEqual(errors, []);
  } finally { releaseVisual(); await context.close(); }
}

(async () => {
  const browser = await chromium.launch({executablePath:process.env.CHROME_PATH, args:['--disable-dev-shm-usage']});
  try {
    await desktop(browser);
    for (const width of [390, 320]) await mobile(browser, width);
    console.log('Reading room passed at 1360, 390, and 320 pixels.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
