'use strict';
// python3 -B android/checks/site_fixture.py | node android/checks/verify_site_reader.cjs
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const rendered = JSON.parse(readFileSync(0, 'utf8'));
const origin = 'https://momonong.me';

async function main() {
  const browser = await chromium.launch({executablePath: process.env.CHROME_BIN, args:['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport:{width:390,height:800}});
    page.setDefaultTimeout(8000);
    let html = rendered.login;
    let loggedIn = false;
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin, 'fixture sends no internet requests');
      switch (url.pathname) {
        case '/knowledge/': return route.fulfill({status: html === rendered.login ? 401 : 200, contentType:'text/html; charset=utf-8', body:html});
        case '/knowledge/access/access.js': return route.fulfill({contentType:'text/javascript', body:rendered.accessScript});
        case '/knowledge/access/access.css': return route.fulfill({contentType:'text/css', body:rendered.accessCss});
        case '/knowledge/access/palette.css':
        case '/knowledge/static/palette.css': return route.fulfill({contentType:'text/css', body:rendered.palette});
        case '/knowledge/access/book.svg':
        case '/knowledge/static/book.svg': return route.fulfill({contentType:'image/svg+xml', body:rendered.book});
        case '/knowledge/access/session': return route.fulfill({json:{configured:true,authenticated:loggedIn,csrf_token:'synthetic-csrf'}});
        case '/knowledge/access/login': {
          assert.equal(route.request().method(),'POST');
          assert.equal(route.request().headers()['x-csrf-token'],'synthetic-csrf');
          if (route.request().postDataJSON().password !== 'synthetic-reader-pass') return route.fulfill({status:401,json:{error:'invalid_login'}});
          html = rendered.complete;
          loggedIn = true;
          return route.fulfill({json:{authenticated:true},headers:{'set-cookie':'synthetic-kh=authenticated; Path=/knowledge/; Secure; HttpOnly; SameSite=Strict'}});
        }
        case '/knowledge/items/1': return route.fulfill({contentType:'text/html; charset=utf-8',body:'<main><h1>固定版本讀者測試頁</h1></main>'});
        case '/knowledge/manage': return route.fulfill({contentType:'text/html; charset=utf-8', body:rendered.manage});
        case '/knowledge/static/reader.css': return route.fulfill({contentType:'text/css',body:rendered.readerCss});
        case '/knowledge/static/briefing.css': return route.fulfill({contentType:'text/css',body:rendered.briefingCss});
        default: return route.fulfill({status:204});
      }
    });
    await page.goto(origin + '/knowledge/');
    assert.equal(await page.locator('header.app-header + main.workspace').count(), 0);
    await page.locator('#login-password').fill('synthetic-incorrect');
    await page.locator('#login-form button').click();
    await page.getByText('密碼不對，請再確認。', {exact:true}).waitFor();
    await page.locator('#login-password').fill('synthetic-reader-pass');
    await page.locator('#login-form button').click();
    await page.getByRole('heading',{name:'今日科技簡報'}).waitFor();
    await page.locator('.wordmark .kh-book').evaluate(image => image.decode());
    for (const [colorScheme, expected] of [['dark', 'rgb(165, 205, 224)'], ['light', 'rgb(40, 79, 101)']]) {
      await page.emulateMedia({colorScheme});
      const background = await page.locator('.briefing-start').first().evaluate(el => getComputedStyle(el).backgroundColor);
      assert.equal(background, expected);
    }
    assert.equal((await page.context().cookies()).find(cookie=>cookie.name==='synthetic-kh').httpOnly,true);

    assert.equal(await page.locator('header.app-header + main.workspace').count(), 1);
    assert.equal(await page.getByRole('navigation', {name:'主要導覽'}).count(), 1);
    const stories = page.locator('article.briefing-story');
    assert.equal(await stories.count(), 5);
    const first = stories.first();
    await first.locator('details.briefing-quick-read > summary').click();
    assert.equal(await first.locator('details[open]').count(), 1);
    await first.locator('h2 > a').click();
    assert.equal(page.url(), origin + '/knowledge/items/1?version=101');
    assert.equal(await page.getByRole('heading').innerText(), '固定版本讀者測試頁');
    await page.goBack();
    await page.getByRole('heading', {name:'今日科技簡報'}).waitFor();
    assert.equal(await page.locator('header.app-header .utility').isVisible(), true);

    const raw = name => readFileSync(path.join(__dirname, '../src/main/res/raw', name + '.js'), 'utf8').trim();
    const header = raw('reader_header') + '(' + JSON.stringify(origin) + ')';
    const navigate = target => raw('reader_navigation') + '(' + JSON.stringify(target) + ',' + JSON.stringify(origin) + ')';
    const css = readFileSync(path.join(__dirname, '../src/main/res/raw/reader_surface_css.css'), 'utf8');
    const surface = raw('reader_surface') + '(' + JSON.stringify(origin) + ',' + JSON.stringify(css) + ')';
    await page.addInitScript({content:header + ';\n' + surface});
    html = rendered.native;
    await page.reload();
    await page.getByRole('heading', {name:'今日科技簡報'}).waitFor();
    assert.equal(await page.locator('header.app-header').isVisible(), false);
    assert.equal(await page.locator('header.app-header .utility').count(), 1);
    // Hold the actual edition GET. Tapping a row's text must already let the reader read.
    let finishArticle;
    const articleGate = new Promise(resolve => { finishArticle = resolve; });
    const edition = origin + '/knowledge/items/2?version=102';
    let editionGets = 0;
    await page.route(edition, async route => {
      editionGets++;
      await articleGate;
      await route.fulfill({contentType:'text/html; charset=utf-8',body:'<main><h1>完整帶讀</h1></main>'});
    });
    const second = page.locator('.daily-story').nth(1);
    const detail = second.locator('details.briefing-quick-read');
    await detail.locator('summary').click();
    assert.equal(await detail.getAttribute('open'), '');
    assert.equal(page.url(), origin + '/knowledge/');
    await detail.locator('summary').click();
    try {
      const [, visible] = await Promise.all([page.waitForRequest(edition,{timeout:2500}),
        second.locator('.briefing-lede').evaluate(el => new Promise(resolve => {
          el.click();
          requestAnimationFrame(() => {
            const reader = document.querySelector('dialog[open]');
            resolve([reader?.querySelector('h1')?.innerText,reader?.querySelector('.kh-passage')?.innerText,
              reader?.checkVisibility() && reader.getBoundingClientRect().height > 0]);
          });
        }))]);
      assert.deepEqual(visible, ['Paper 2', 'Change', true]);
    } finally { finishArticle(); }
    await page.getByRole('heading',{name:'完整帶讀'}).waitFor();
    assert.equal(page.url(), edition);
    assert.equal(editionGets, 1);
    await page.goBack();
    await page.getByRole('heading',{name:'今日科技簡報'}).waitFor();
    assert.equal(await page.getByRole('heading',{name:'Paper 2',level:1}).count(),0);
    const expiredEdition = origin + '/knowledge/items/3?version=103';
    let challengeSeen = false;
    await page.route(expiredEdition, route => {
      if (!challengeSeen) {
        loggedIn = false;
        challengeSeen = true;
      }
      return loggedIn ? route.fulfill({contentType:'text/html; charset=utf-8',body:'<main><h1>重新登入後的文章</h1></main>'}) :
        route.fulfill({status:401,contentType:'text/html; charset=utf-8',body:rendered.login});
    });
    await page.locator('.daily-story').nth(2).locator('h2 a').click();
    await page.locator('#login-password').waitFor();
    assert.equal(await page.locator('dialog[open]').count(),0);
    await page.locator('#login-password').fill('synthetic-reader-pass');
    await page.locator('#login-form button').click();
    await page.getByRole('heading',{name:'重新登入後的文章'}).waitFor();
    assert.equal(page.url(),expiredEdition);
    html = rendered.native;
    await page.goBack();
    await page.getByRole('heading',{name:'今日科技簡報'}).waitFor();
    assert.equal(await page.evaluate(navigate(origin + '/knowledge/settings/')), false);
    await Promise.all([page.waitForURL(origin + '/knowledge/manage'), page.evaluate(navigate(origin + '/knowledge/manage'))]);
    assert.equal(await page.getByRole('heading', {name:'管理'}).isVisible(), true);
    assert.equal(await page.locator('header.topbar .brand').isVisible(), false);
    await page.getByText('工具', {exact:true}).click();
    assert.equal(await page.getByRole('link', {name:'搜尋',exact:true}).isVisible(), true);

    html = rendered.login;
    loggedIn = false;
    await page.goto(origin + '/knowledge/');
    await page.locator('#login-password').waitFor();
    assert.equal(await page.locator('header .brand').isVisible(), false);
    assert.equal(await page.getByRole('link', {name:'裝置管理'}).isVisible(), true);
    process.stdout.write('Passed browser login and reading, app header, management navigation, library tools and device access.\n');
  } finally { await browser.close(); }
}

main().catch(error => { console.error(error); process.exitCode=1; });
