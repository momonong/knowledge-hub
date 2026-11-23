'use strict';
// Supply Playwright through NODE_PATH, and CHROME_PATH for a system Chromium.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');

const [db, manifestPath, output] = process.argv.slice(2);
if (!db || !manifestPath || !output) throw Error('Usage: node verify_phase1_browser.cjs db originals.json output-directory');
const base = 'https://kh-phase1.test/knowledge/';
const edited = '我想先量測固定檢索預算，再比較分組前後的證據覆蓋率。';
const unsent = '換裝置後想接著問：同一時間窗的紀錄如何分組？';
const receipt = {papers: [], layouts: [], checks: [], javascriptErrors: []};
let browser, server, work;

async function start(port = 0, provider = false) {
  server = spawn(process.env.KH_PYTHON || 'python3.12', ['-B','-m','checks.integration.phase1_smoke_host',
    path.resolve(db),work,'--port',String(port),...(provider ? ['--test-provider'] : [])], {stdio:['ignore','pipe','inherit']});
  const lines = createInterface({input:server.stdout});
  try {
    for await (const line of lines) {
      const value = Number(line);
      if (Number.isInteger(value) && value > 0) return value;
    }
    throw Error('Loopback HTTPS server exited before startup');
  } finally { lines.close(); }
}

async function stop() {
  if (server && server.exitCode === null && server.signalCode === null) {
    const done = once(server,'exit');
    server.kill('SIGTERM');
    await done;
  }
}

async function login(width, height) {
  const context = await browser.newContext({viewport:{width, height}, ignoreHTTPSErrors:true});
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', error => receipt.javascriptErrors.push(error.message));
  const unauthenticated = await page.goto(base);
  assert.equal(unauthenticated.status(),401);
  await page.locator('#login-password').fill(await fs.readFile(path.join(work,'browser-credential'),'utf8'));
  await page.locator('#login-form button').click();
  await page.waitForSelector('[data-daily]');
  return {context, page};
}

async function layout(page, label) {
  const sizes = await page.evaluate(() => ({width:innerWidth, documentWidth:document.documentElement.scrollWidth}));
  assert(sizes.documentWidth <= sizes.width + 1,'Horizontal overflow on ' + label + ': ' + JSON.stringify(sizes));
  receipt.layouts.push({label,...sizes});
  await page.screenshot({path:path.join(output,label + '.png')});
}

async function saved(page) {
  await page.waitForFunction(() => /已保存/.test(document.querySelector('#draft-status')?.textContent || ''));
}

async function openDrawer(page) {
  const drawer = page.locator('[data-room-drawer]');
  if (!await drawer.isVisible()) await page.locator('[data-room-ask]').click();
  assert.equal(await page.locator('[data-room-ask]').getAttribute('aria-expanded'), 'true');
  assert(await drawer.isVisible());
}

async function closeDrawer(page) {
  const drawer = page.locator('[data-room-drawer]');
  await drawer.locator('[data-room-close]').click();
  await drawer.waitFor({state:'hidden'});
  assert.equal(await page.locator('[data-room-ask]').getAttribute('aria-expanded'), 'false');
}

async function bookmark(page, readerUrl, anchor) {
  const saved = page.waitForResponse(response => response.url().endsWith('/reading') && response.ok() &&
    new URLSearchParams(response.request().postData()).get('anchor') === anchor);
  await page.goto(readerUrl + '#' + encodeURIComponent(anchor));
  await saved;
}

async function selectText(page, locator, length = 50) {
  await locator.scrollIntoViewIfNeeded();
  return locator.evaluate((element, length) => {
    const text = element.firstChild;
    const range = document.createRange();
    range.setStart(text,0);
    range.setEnd(text,Math.min(length,text.length));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
    return selection.toString().trim();
  },length);
}

async function ask(page, question) {
  await openDrawer(page);
  await page.locator('#followup').fill(question);
  const response = page.waitForResponse(r => r.url().includes('/followup') && r.request().method() === 'POST');
  await page.locator('[data-layer-send]').click();
  assert.equal((await response).headers()['content-type'].split(';')[0],'text/event-stream');
  const turn = page.locator('.layer-turn').filter({has:page.locator('.chat-question',{hasText:question})}).last();
  await turn.locator('[data-layer-kind="inference"]').waitFor();
  assert.deepEqual(await turn.locator('[data-layer-kind]').evaluateAll(rows => rows.map(r=>r.dataset.layerKind)),
    ['evidence','limits','inference']);
  return turn;
}

async function main() {
  work = await fs.mkdtemp(path.join(os.tmpdir(),'kh-phase1-browser-'));
  await fs.mkdir(output,{recursive:true});
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=kh-phase1.test',
    '-addext','subjectAltName=DNS:kh-phase1.test','-keyout',path.join(work,'local.key'),'-out',path.join(work,'local.crt')],{stdio:'ignore'});
  const papers = JSON.parse(await fs.readFile(manifestPath,'utf8'));
  const port = await start();
  browser = await chromium.launch({executablePath:process.env.CHROME_PATH || undefined, headless:true,
    args:['--no-proxy-server','--host-resolver-rules=MAP kh-phase1.test:443 127.0.0.1:' + port]});
  const desktop = await login(1366,900);
  const page = desktop.page;
  const stories = page.locator('.daily-story');
  assert.equal(await stories.count(),5);
  await layout(page,'desktop-home');
  await page.setViewportSize({width:768,height:1024});
  await layout(page,'tablet-home');
  await page.setViewportSize({width:1366,height:900});
  const card = stories.first();
  assert.equal(await card.locator('.daily-quick-read').getAttribute('open'),null);
  let requests = 0;
  const countRequest = () => {requests++;};
  page.on('request',countRequest);
  await card.locator('.daily-quick-read > summary').focus();
  await page.keyboard.press('Enter');
  assert.equal(await card.locator('.daily-l2 dt').count(),4);
  assert(await card.locator('.daily-l2').isVisible());
  page.off('request',countRequest);
  assert.equal(requests,0);
  for (const action of ['skip','undo','later','undo']) {
    const posted = page.waitForResponse(r=>r.url().includes('/daily-choice') && r.request().method()==='POST');
    await Promise.all([page.waitForNavigation({waitUntil:'domcontentloaded'}),card.locator('[name=choice][value=' + action + ']').click()]);
    const postedResponse = await posted;
    assert.equal(postedResponse.status(),303,action + ' origin=' + (await postedResponse.request().allHeaders()).origin +
      ' ' + (await page.locator('body').innerText()).slice(0,80));
  }
  const fixedLink = card.getByRole('link',{name:'開始一起讀',exact:true});
  const fixedPath = await fixedLink.getAttribute('href');
  assert.match(fixedPath,/^\/knowledge\/items\/\d+\?version=\d+$/);
  const fixedNavigation = page.waitForRequest(r=>r.isNavigationRequest() && r.url().endsWith(fixedPath));
  await Promise.all([page.waitForURL(new URL(fixedPath,base).href),fixedLink.click()]);
  assert.equal((await fixedNavigation).method(),'GET');
  receipt.checks.push('five dated cards, keyboard L2 opens without requests, skip/later/undo, fixed-edition reading link');

  for (const paper of papers) {
    const url = base + 'items/' + paper.item_id + '?version=' + paper.version_id;
    await page.goto(base);
    const open = page.locator('#daily-' + paper.item_id).getByRole('button',{name:'取得全文並更新速讀',exact:true});
    await Promise.all([page.waitForURL(current => current.pathname === '/knowledge/items/' + paper.item_id
      && /^\d+$/.test(current.searchParams.get('version') || '')),open.click()]);
    const acquired = await page.locator('[data-reader]').getAttribute('data-version');
    assert.equal(await page.locator('[data-room-source] .room-source-links').getByRole('link',
      {name:'出版者原文',exact:false}).getAttribute('href'),paper.html);
    // Re-parsing historical MathML can create a new immutable extraction of the same
    // publisher edition. The saved original must still open at its original local ID.
    if (String(acquired) !== String(paper.version_id)) await page.goto(url);
    assert.equal(await page.locator('[data-reader]').getAttribute('data-version'),String(paper.version_id));
    const guide = page.locator('[data-layer-guide-content]');
    await guide.locator('a').first().waitFor({state:'attached'});
    const left = page.locator('#panel-lesson');
    const right = page.locator('#panel-original');
    assert(await left.isVisible() && await right.isVisible());
    assert((await left.boundingBox()).x < (await right.boundingBox()).x);
    const citations = guide.locator('.guide-citations:visible').first();
    await citations.locator(':scope > summary').click();
    const cite = citations.locator('a[href*="/knowledge/items/"][href*="#"]').first();
    const href = new URL(await cite.getAttribute('href'), base);
    assert.equal(href.searchParams.get('version'), String(paper.version_id));
    const target = decodeURIComponent(href.hash.slice(1));
    assert(await page.evaluate(id=>!!document.getElementById(id)?.closest('#panel-original'),target));
    await page.evaluate(() => {window.phase1ReaderDocument = true;});
    await cite.click();
    await page.waitForFunction(id=>{
      const block = document.getElementById(id);
      const pane = block?.closest('#panel-original');
      const rect = block?.getBoundingClientRect();
      const frame = pane?.getBoundingClientRect();
      return window.phase1ReaderDocument && location.hash === '#'+encodeURIComponent(id) &&
        block?.classList.contains('room-highlight') && rect.bottom > Math.max(frame.top, 0) &&
        rect.top < Math.min(frame.bottom, innerHeight);
    },target);
    assert(await left.isVisible());
    const tables = right.locator('.visual-card').filter({has:page.locator('table')});
    await tables.first().waitFor({state:'attached',timeout:65000});
    const figure = right.locator('.visual-card').filter({
      has:page.locator('summary').filter({hasText:/^圖 /})
    }).first();
    assert.match(await figure.getAttribute('id'), /^kh-visual-/);
    await figure.locator(':scope > summary').evaluate(el=>{el.parentElement.open=true;});
    const picture = figure.locator('img').first();
    await picture.scrollIntoViewIfNeeded({timeout:65000});
    await page.waitForFunction(id => Array.from(document.getElementById(id).querySelectorAll('img')).some(i=>i.complete && i.naturalWidth>0),
      await figure.getAttribute('id'),{timeout:65000});
    const pdf = await page.locator('a[href*="/pdf?version="]').first().evaluate(async link => {
      const response = await fetch(link.href);
      const raw = await response.arrayBuffer();
      return {status:response.status,mime:response.headers.get('Content-Type'),bytes:raw.byteLength,
        magic:new TextDecoder().decode(raw.slice(0,5))};
    });
    assert.equal(pdf.status,200);
    assert.equal(pdf.mime,'application/pdf');
    assert.equal(pdf.magic,'%PDF-');
    const opened = page.waitForEvent('popup');
    await page.locator('a[href*="/pdf?version="]').first().click();
    const viewer = await opened;
    await viewer.waitForURL('**/pdf?version=' + paper.version_id);
    await viewer.close();
    const guideSource = await guide.locator('[data-layer-quote]').count();
    assert(guideSource > 0);
    receipt.papers.push({key:paper.key, version:paper.version_id, acquiredVersion:acquired, htmlBlocks:await page.locator('.source-paragraph').count(),
      tables:await tables.count(), verifiedGuideTarget:target, loadedFigure:true, pdfBytes:pdf.bytes});
  }
  const paper = papers[0];
  const readerUrl = base + 'items/' + paper.item_id + '?version=' + paper.version_id;
  const guideUrl = base + 'items/' + paper.item_id + '/guide?version=' + paper.version_id;
  const guideResponse = page.waitForResponse(response => response.url() === guideUrl && response.ok());
  await page.goto(readerUrl);
  const localGuide = await (await guideResponse).json();
  await page.locator('[data-layer-guide-content] a').first().waitFor({state:'attached'});
  await layout(page,'desktop-reader');
  const guideQuote = page.locator('[data-layer-guide-content] .prose[data-layer-quote]:visible').first();
  const selectedGuide = await selectText(page,guideQuote,28);
  const storedGuide = page.waitForResponse(r=>r.url().includes('/captures') && r.request().method()==='POST');
  await page.locator('.layer-selection-tools').getByRole('button',{name:'想法',exact:true}).click();
  assert.equal((await storedGuide).status(),200);
  await page.locator('.layer-selection-receipt').getByText('已保存 #想法 標記。').waitFor();
  assert.equal(await page.locator('[data-room-drawer]').isVisible(), false);
  await openDrawer(page);
  await page.locator('.capture-quotation').filter({hasText:selectedGuide}).waitFor();
  const turn = await ask(page,'RecordFolder 如何組合記錄？');
  await turn.getByRole('button',{name:'留下這份理解'}).click();
  const draft = page.locator('[data-layer-capture-list] textarea').first();
  await draft.waitFor();
  assert((await draft.inputValue()).length > 10);
  await draft.fill(edited);
  await page.locator('[data-layer-capture-list]').getByRole('button',{name:'儲存草稿'}).first().click();
  await page.waitForFunction(() => /已保存/.test(document.querySelector('[data-layer-capture-status]').textContent));
  const absent = await ask(page,'原文是否給出量子電腦的型號與量子位元數？');
  assert(await absent.locator('[data-layer-missing]').count() > 0);
  await absent.getByRole('button',{name:'轉存待查'}).click();
  await page.waitForFunction(() => document.querySelector('[data-layer-capture-list]').textContent.includes('量子'));

  await closeDrawer(page);
  assert(await page.locator('#panel-original').isVisible());
  const passage = page.locator('.source-paragraph .prose').filter({hasText:'RecordFolder'}).first();
  let anchor = await passage.locator('..').getAttribute('data-anchor');
  let originalQuote;
  for (const name of ['想法','盲點／待查','可試看看']) {
    originalQuote = await selectText(page,passage);
    const savedTag = page.waitForResponse(r=>r.url().includes('/captures') && r.request().method()==='POST');
    await page.locator('.layer-selection-tools').getByRole('button',{name,exact:true}).click();
    assert.equal((await savedTag).status(),200);
  }
  assert.equal(await page.locator('[data-room-drawer]').isVisible(), false);
  assert.equal(await selectText(page,passage),originalQuote);
  await page.locator('.layer-selection-tools').getByRole('button',{name:'問這段',exact:true}).click();
  assert(await page.locator('[data-room-drawer]').isVisible());
  assert.equal(await page.locator('[data-layer-focus-quote]').textContent(),originalQuote);
  assert(await page.locator('#followup').evaluate(el=>el===document.activeElement));
  await page.locator('#followup').fill(unsent);
  await saved(page);
  await closeDrawer(page);
  await bookmark(page,readerUrl,anchor);
  // Use the saved server position that the browser acknowledged, including the precise block.
  const stored = await page.evaluate(async () => (await (await fetch('/knowledge/export.json')).json()).reading_sessions);
  const before = stored.find(row=>String(row.version_id)===String(paper.version_id) && row.item_id===paper.item_id);
  assert.equal(before.question_draft,unsent);
  assert.equal(before.tab,'original');
  assert.equal(before.anchor,anchor);
  await desktop.context.close();
  await stop();
  await start(port);

  const mobile = await login(390,844);
  await layout(mobile.page,'phone-home');
  const phone = mobile.page;
  await phone.goto(readerUrl);
  assert.equal(await phone.locator('#followup').inputValue(),unsent);
  assert.equal(await phone.locator('[data-reader]').getAttribute('data-anchor'),anchor);
  assert.equal(await phone.locator('[data-tab-button=original]').getAttribute('aria-selected'),'true');
  assert(await phone.locator('#panel-original').isVisible());
  assert.equal(await phone.locator('#panel-lesson').isVisible(),false);
  assert.equal(await phone.locator('[data-room-drawer]').isVisible(),false);
  assert.equal(await phone.locator('.capture-quotation').filter({hasText:originalQuote}).count(),3);
  await phone.waitForFunction(() => ['kh-idea','kh-question','kh-try'].every(name=>CSS.highlights?.get(name)?.size>0));
  await openDrawer(phone);
  assert.equal(await phone.locator('[data-layer-capture-list] textarea').first().inputValue(),edited);
  await phone.locator('[data-layer-capture-list]').getByRole('button',{name:'確認為我的理解'}).first().click();
  await phone.locator('[data-layer-capture-list] .prose').filter({hasText:edited}).waitFor();
  for (const name of ['想法','盲點','可試看看']) assert((await phone.locator('[data-layer-capture-list]').innerText()).includes(name));
  await phone.locator('[data-layer-recall] > summary').click();
  await phone.locator('[data-layer-recall-form] input').fill('檢索預算');
  await phone.locator('[data-layer-recall-form] button').click();
  await phone.locator('[data-layer-recall-results] a').first().waitFor();
  await layout(phone,'phone-drawer');
  await closeDrawer(phone);
  await layout(phone,'phone-reader');
  await phone.setViewportSize({width:768,height:1024});
  await layout(phone,'tablet-reader');
  for (const selector of ['.layer-turn','.layer-capture']) {
    const target = await phone.locator(selector).first().getAttribute('id');
    await bookmark(phone,readerUrl,target);
    await phone.goto(base);
    await phone.goto(readerUrl);
    assert.equal(await phone.locator('[data-reader]').getAttribute('data-anchor'),target);
    await phone.waitForFunction(id => {
      const selected = document.getElementById(id);
      const box = selected?.getBoundingClientRect();
      const drawer = selected?.closest('[data-room-drawer]');
      const frame = drawer?.getBoundingClientRect();
      return drawer && !drawer.hidden && box.top < Math.min(frame.bottom, innerHeight) && box.bottom > Math.max(frame.top, 0);
    },target,{timeout:5000}).catch(async () => {
      const geometry = await phone.locator('#'+target).evaluate(node=>({
        top:node.getBoundingClientRect().top, bottom:node.getBoundingClientRect().bottom, scroll:scrollY,
        tab:document.querySelector('[data-reader]').dataset.tab, hidden:!!node.closest('[hidden]'),
      }));
      throw Error('Saved bookmark '+target+' outside viewport '+JSON.stringify(geometry));
    });
  }
  await mobile.context.close();
  await stop();
  await start(port,true);
  const recovery = await login(1366,900);
  const retryPage = recovery.page;
  // L3 is already exercised above. Its automatic request otherwise consumes
  // the controlled provider's first-call timeout intended for the L4 test.
  await retryPage.route(guideUrl, route => route.fulfill({json:localGuide}));
  await retryPage.goto(readerUrl);
  await openDrawer(retryPage);
  await retryPage.locator('#followup').fill('RecordFolder 的長短期記錄怎麼分組？');
  await retryPage.locator('[data-layer-send]').click();
  const latest = retryPage.locator('.layer-turn').last();
  await latest.getByText('模型在 15 秒內未開始回覆。',{exact:true}).waitFor({timeout:23000});
  await latest.getByRole('button',{name:'重試這題'}).click();
  await latest.getByRole('button',{name:'切換備援模型'}).waitFor();
  assert.match(await latest.innerText(),/引文|引用/);
  assert.equal(await latest.locator('[data-layer-kind]').count(),0);
  await latest.getByRole('button',{name:'切換備援模型'}).click();
  await latest.locator('[data-layer-kind=inference]').waitFor();
  assert((await latest.innerText()).includes('備援模型'));
  assert.equal(await latest.locator('[data-layer-kind]').count(),3);
  await recovery.context.close();
  receipt.checks.push('L4 positive/missing, three original tags, persisted draft, confirmation, passive Chinese search',
    'two independent KH logins and browser contexts, HTTPS Secure cookies, complete server-process restart',
    'unsent question, reading tab and position, edited understanding, three original highlights restored',
    'daily deep-read forms, side-by-side guide and highlighted sources, fixed PDF viewer, selected L3 and original quote, opened L4 drawer and durable L4/capture bookmarks',
    'controlled HTTP primary: visible 15-second timeout, retry rejects foreign citation, explicit fallback completes three sections');
  assert.deepEqual(receipt.javascriptErrors,[]);
  await fs.writeFile(path.join(output,'browser.json'),JSON.stringify(receipt,null,2));
  console.log(JSON.stringify(receipt,null,2));
}

main().catch(error => {
  console.error(String(error.message).replace(/(?:set-cookie|cookie|authorization):[^\n]+/gi,'[private header removed]'));
  process.exitCode=1;
}).finally(async () => {
  if (browser) await browser.close();
  await stop();
  if (work) await fs.rm(work,{recursive:true,force:true});
});
