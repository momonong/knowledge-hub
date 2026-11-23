'use strict';
// Run against a disposable, seeded loopback test database. These actions create real private captures.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const [base, casesFile, dest] = process.argv.slice(2);
if (!base || !casesFile || !dest || !['127.0.0.1', 'localhost'].includes(new URL(base).hostname))
  throw Error('Usage: node verify_v11_experience.cjs http://127.0.0.1:PORT cases.json output-directory');
const save = (name,data) => fs.writeFile(path.join(dest,name+'.json'),JSON.stringify(data,null,2));
const screenshot = (page,name) => page.screenshot({path:path.join(dest,name+'.png')});

async function navigate(page, id) {
  await page.goto(base);
  const pending = page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/guide'));
  await page.locator('#daily-'+id+' .briefing-start').click();
  const guide = await (await pending).json();
  assert.equal(guide.mode,'model',JSON.stringify(guide.model));
  await page.locator('[data-guide-chip=mechanism]').waitFor({state:'attached'});
  assert.equal(await page.locator('.guide-chip').count(),3);
  assert.deepEqual(await page.locator('[data-guide-stage]').evaluateAll(nodes=>nodes.map(n=>n.dataset.guideStage)),
    ['hook','mechanism','evidence','chips']);
  return guide;
}

async function ask(page, role, prompt) {
  const chip = page.locator('[data-guide-chip=mechanism]');
  const question = role==='mechanism' ? (await chip.textContent()).trim() : prompt;
  const pending = page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname.endsWith('/followup'));
  if (role==='mechanism') await chip.click();
  else {
    await page.locator('#followup').fill(question);
    await page.locator('[data-layer-send]').click();
  }
  const res = await pending;
  assert.equal(res.status(),200);
  assert.match(res.headers()['content-type'],/text\/event-stream/);
  const frames=(await res.text()).trim().split(/\r?\n\r?\n/).map(frame=>{
    const [name,payload]=frame.split(/\r?\n/);
    return {event:name.slice('event: '.length),...JSON.parse(payload.slice('data: '.length))};
  });
  const done=frames.at(-1);
  const sections=frames.filter(e=>e.event==='section');
  const record={role,question,done,sections};
  assert.equal(frames[0].mode,'model');
  if(done.event!=='done')return record;
  const el=page.locator('#l4-'+done.id);
  await el.locator('[data-layer-kind=inference]').waitFor();
  assert.deepEqual(sections.map(e=>e.kind),['evidence','limits','inference']);
  assert(done.timing.ttft_ms>0 && done.timing.first_section_ms>=done.timing.ttft_ms && done.timing.total_ms<45000);
  if(role==='boundary') assert(sections.every(e=>e.missing && !e.citations.length));
  return record;
}

async function main() {
  const cases=JSON.parse(await fs.readFile(casesFile,'utf8'));
  assert(cases.length>=2);
  await fs.mkdir(dest,{recursive:true});
  const browser=await chromium.launch({executablePath:process.env.CHROME_PATH,headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:900}});
    const page=await context.newPage();
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(60000);
    await page.goto(base);
    assert.equal(await page.locator('.daily-story').count(),5);
    await screenshot(page,'desktop-home');
    for (const c of cases){
      const guide=await navigate(page,c.item);await save('guide-'+c.item,guide);
      const refs=page.locator('[data-knowledge-connections] [data-knowledge-kind]');
      assert((await refs.count())>=2 && (await refs.count())<=4);
      const related=await refs.evaluateAll(els=>els.map(el=>({kind:el.dataset.knowledgeKind,
        reason:el.querySelector('[data-connection-reason]').textContent,href:el.querySelector('[data-connection-destination]').getAttribute('href')})));
      await save('related-'+c.item,related);
      await screenshot(page,'desktop-room-'+c.item);
      const fig=page.locator('[data-guide-visual-link]');const hash=decodeURIComponent((await fig.getAttribute('href')).split('#').pop());
      await fig.click();await page.waitForFunction(id=>document.getElementById(id)?.open,hash);
      await page.waitForFunction(id=>{
        const figure = document.getElementById(id);
        const panel = figure.closest('[data-room-source]').getBoundingClientRect();
        return figure.classList.contains('room-highlight') && [...figure.querySelectorAll('img,table')].some(media=>{
          const box = media.getBoundingClientRect();
          return (media.tagName==='TABLE' || media.complete && media.naturalWidth>0)
            && box.bottom>Math.max(0,panel.top) && box.top<Math.min(innerHeight,panel.bottom);
        });
      },hash,{timeout:60000});
      await screenshot(page,'desktop-figure-'+c.item);
      const records=[];
      for(const role of ['mechanism','comparison','boundary']){
        const rec=await ask(page,role,c[role]);records.push(rec);await save('turns-'+c.item,records);
        assert.equal(rec.done.event,'done',JSON.stringify(rec.done));
        console.log(c.item,role,rec.done.timing,rec.sections.map(s=>s.citations.length));
        const turn=page.locator('#l4-'+rec.done.id);
        if (role==='comparison' || role==='boundary'){
          const action=turn.getByRole('button',{name:role==='comparison'?'留下這份理解':'轉存待查',exact:true});
          const saved=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname.endsWith('/captures'));
          await action.click();const record=(await (await saved).json()).record;assert(record.id);
        }
        if(role==='comparison')await screenshot(page,'desktop-followup-'+c.item);
      }
    }
    const phone=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
    const mobile=await phone.newPage();mobile.setDefaultTimeout(60000);
    mobile.on('pageerror',e=>errors.push(e.message));await mobile.goto(base);await screenshot(mobile,'phone-home');
    const selected=cases[0];await navigate(mobile,selected.item);
    await mobile.locator('[data-tab-button=lesson]').click();
    await screenshot(mobile,'phone-room');
    const first=mobile.locator('[data-knowledge-kind]').first();
    let parent=mobile.locator('details[data-connections-disclosure]');
    if(await parent.count() && !await parent.evaluate(el=>el.open))await parent.locator(':scope > summary').click();
    await first.locator(':scope > summary').click();
    assert(await first.locator('[data-connection-destination]').isVisible());
    await screenshot(mobile,'phone-connections');
    await mobile.locator('[data-guide-visual-link]').click();assert(await mobile.locator('#panel-original').isVisible());
    for(const width of [390,320]){await mobile.setViewportSize({width,height:844});
      assert.equal(await mobile.evaluate(()=>document.documentElement.scrollWidth),width);}
    await mobile.locator('[data-room-ask]').click();assert(await mobile.locator('[data-layer-capture-list]').locator('textarea').count());
    assert.match(await mobile.locator('[data-layer-capture-list]').innerText(),/待查/);
    assert.deepEqual(errors,[]);await save('checks',{cases:cases.map(c=>c.item),errors,widths:[1440,390,320],freshBrowserContext:true});
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
