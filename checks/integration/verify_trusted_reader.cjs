// Uses isolated browser profiles, real WebAuthn signatures and an in-memory virtual device per phone.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');
const {chromium} = require('playwright');

const hostname = 'reader.test';
const origin = 'https://' + hostname;
const project = path.resolve(__dirname, '../..');

async function main() {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(),'kh-trusted-test-'));
  let host, browser;
  try {
    execFileSync('openssl', [
      'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-subj', '/CN=' + hostname, '-addext', 'subjectAltName=DNS:' + hostname,
      '-out', path.join(temp, 'tls.crt'), '-keyout', path.join(temp, 'tls.key'),
    ], {stdio: 'ignore'});
    host = spawn(process.env.KH_TEST_PYTHON || 'python3', [path.join(__dirname,'trusted_reader_fixture.py'),temp],{
      env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:process.env.KH_TEST_PROJECT || project},
      stdio:['ignore','pipe','inherit'],
    });
    await Promise.race([once(host.stdout,'data'), once(host,'exit').then(() => {throw Error('Fixture failed to start');})]);
    const fixture = JSON.parse(await fs.readFile(path.join(temp,'fixture.json'),'utf8'));
    browser = await chromium.launch({
      executablePath: process.env.CHROME_BIN || undefined,
      args: [
        '--no-proxy-server',
        '--host-resolver-rules=MAP ' + hostname + ' 127.0.0.1:' + fixture.port + ', MAP * ~NOTFOUND',
      ],
    });
    const context = async (storageState) => {
      const result = await browser.newContext({
        ignoreHTTPSErrors: true, serviceWorkers: 'block', viewport: {width: 390, height: 844}, storageState,
      });
      await result.route(url => url.origin !== origin, route => route.abort());
      result.setDefaultTimeout(8000);
      return result;
    };
    const pageErrors=[];
    async function checkLayout(page, name) {
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), name+' overflows');
      if (process.env.KH_TEST_SCREENSHOTS) {
        await fs.mkdir(process.env.KH_TEST_SCREENSHOTS,{recursive:true});
        await page.screenshot({path:path.join(process.env.KH_TEST_SCREENSHOTS,name+'.png')});
      }
    }
    async function phone() {
      const ctx=await context();
      const page=await ctx.newPage();
      page.on('pageerror',error=>pageErrors.push(error.message));
      const cdp=await ctx.newCDPSession(page);
      await cdp.send('WebAuthn.enable');
      await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',
        hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
      return {ctx,page};
    }
    async function enroll(device, name) {
      const p=device.page;
      const first=await p.goto(origin+fixture.deepLink);
      assert.equal(first.headers()['x-kh-test-fixture'],'trusted-reader-loopback');
      assert.equal((await first.serverAddr()).ipAddress,'127.0.0.1');
      await p.getByLabel('信任這支裝置').waitFor();
      await checkLayout(p,'login');
      await p.locator('#login-password').fill(fixture.reader);
      await p.getByLabel('信任這支裝置').check();
      await p.getByRole('button',{name:'登入 Knowledge Hub',exact:true}).click();
      await p.getByLabel('裝置名稱').fill(name);
      await checkLayout(p,'enrollment');
      await p.getByRole('button',{name:'建立通行金鑰'}).click();
      await p.waitForSelector('main.workspace');
      assert.equal(p.url(),origin+fixture.deepLink);
      const session=await p.evaluate(async () => {
        const response=await fetch('/knowledge/access/session',{cache:'no-store'});
        if(response.headers.get('x-kh-test-fixture') !== 'trusted-reader-loopback') throw Error('Expected loopback');
        const {trusted_device,trusted_until}=await response.json();
        return {trusted_device,trusted_until};
      });
      assert.equal(session.trusted_device.name,name);
      assert.ok(session.trusted_until>Date.now()/1000+29*86400);
    }
    const one=await phone(), two=await phone();
    await enroll(one,'第一支測試手機');
    await enroll(two,'第二支測試手機');
    console.log('Both virtual phones enrolled through the reader login form.');
    // A newly opened browser context with only the saved cookies can return straight to the same article.
    const reopened=await context(await one.ctx.storageState());
    const resumed=await reopened.newPage();
    assert.equal((await resumed.goto(origin+fixture.deepLink)).status(),200);
    await resumed.waitForSelector('main.workspace');
    await reopened.close();
    await one.ctx.clearCookies();
    assert.equal((await one.page.reload()).status(),401);
    await one.page.getByRole('button',{name:'用指紋或螢幕鎖定登入'}).click();
    await one.page.waitForSelector('main.workspace');
    assert.equal(one.page.url(),origin+fixture.deepLink);
    console.log('Trusted reopen and passkey reauthentication both resumed the fixed article.');

    const owner=await context();
    await owner.addCookies([{name:'kh_fixture_owner',value:fixture.owner,url:origin,httpOnly:true,secure:true}]);
    const settings=await owner.newPage();
    await settings.goto(origin+'/knowledge/settings/');
    const device=settings.locator('#trusted-device-list li').filter({hasText:'第一支測試手機'});
    await device.waitFor();
    await checkLayout(settings,'manage-devices');
    await device.getByRole('button',{name:'撤銷'}).click();
    await device.getByRole('button',{name:'確認撤銷'}).click();
    await device.waitFor({state:'detached'});
    assert.equal((await one.page.reload()).status(),401);
    assert.equal((await two.page.reload()).status(),200);
    await one.page.getByRole('button',{name:'用指紋或螢幕鎖定登入'}).click();
    await one.page.waitForFunction(()=>document.querySelector('#result')?.textContent.includes('通行金鑰') &&
      !document.querySelector('#passkey-login').disabled);
    const denied=await one.page.reload();
    assert.equal(denied.headers()['x-kh-test-fixture'],'trusted-reader-loopback');
    assert.equal(denied.status(),401);
    assert.deepEqual(pageErrors,[]);
    console.log('PASS two virtual devices, trusted reopen, WebAuthn reauthentication, article URL, individual revocation and revoked credential rejection.');
  } finally {
    if (browser) await browser.close();
    if (host && host.exitCode === null && host.signalCode === null) {
      const closed = once(host, 'exit');
      host.kill();
      await closed;
    }
    await fs.rm(temp,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
