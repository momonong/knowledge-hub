// Execute the browser script with controlled save requests. A successful
// autosave must apply exactly one navigation, and a failed save permits retry.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] || 'knowledge_hub/static/reader.js', 'utf8');
const origin = 'http://127.0.0.1:8765';

function launch() {
  const events = {}, documentEvents = {}, formEvents = {};
  const assignments = [], requests = [];
  const field = {dataset:{draft:'question_draft'},value:'unsaved question',addEventListener(){}};
  const status = {textContent:'',toggleAttribute(){}};
  const reader = {dataset:{revision:'0',tab:'lesson',anchor:'',item:'1',version:'1'},
    querySelectorAll(selector){return selector === '[data-draft]' ? [field] : [];},
    querySelector(selector){return selector === '#followup' ? field : null;}};
  const form = {addEventListener(name,fn){formEvents[name]=fn;},reportValidity(){return true;}};
  const document = {
    querySelector(selector){return ({'[data-reader]':reader,'#page-csrf':{value:'csrf'},'#draft-status':status})[selector] || null;},
    querySelectorAll(selector){return selector === 'form[data-busy]' ? [form] : selector === '[data-draft]' ? [field] : [];},
    getElementById(){return null;},addEventListener(name,fn){documentEvents[name]=fn;}
  };
  vm.runInNewContext(source,{
    document,window:{addEventListener(name,fn){events[name]=fn;}},
    location:{hash:'',href:origin+'/items/1',origin,pathname:'/items/1',search:'',
      assign(href){assignments.push(href);}},
    fetch:()=>new Promise(resolve=>requests.push(resolve)), URLSearchParams,URL,Number,
    setTimeout(){return 1;},clearTimeout(){},requestAnimationFrame(){}
  });
  function click(path, modifiers={}) {
    let prevented=false;
    const done = documentEvents.click({target:{closest:()=>({href:origin+path,target:''})}, ...modifiers,
      preventDefault(){prevented=true;}});
    return {prevented,done};
  }
  function protectedUnload() {
    let prevented=false;
    events.beforeunload({preventDefault(){prevented=true;}});
    return prevented;
  }
  return {events,formEvents,assignments,requests,status,click,protectedUnload};
}

async function verify() {
  const submit = launch();
  assert.equal(submit.protectedUnload(), true, 'dirty state must warn before leaving');
  submit.formEvents.submit({preventDefault(){}});
  assert.equal(submit.status.textContent,'正在保存到本機…');
  assert.equal(submit.protectedUnload(),true,'submitting must retain the unfinished-save warning');
  assert.equal(submit.click('/').prevented,true,'navigation must wait for the pre-submit save');

  const nav = launch();
  const first = nav.click('/reading');
  assert.equal(first.prevented, true);
  assert.equal(nav.click('/library').prevented, true);
  assert.equal(nav.click('/items/1#abstract').prevented, false, 'same-page evidence links retain hash navigation');
  for (const key of ['ctrlKey','metaKey','shiftKey','altKey']) {
    assert.equal(nav.click('/library', {[key]:true}).prevented, false);
  }
  assert.equal(nav.requests.length, 1);
  nav.requests[0]({ok:true,json:async()=>({revision:1})});
  await first.done;
  assert.deepEqual(nav.assignments,[origin+'/reading']);
  assert.equal(nav.click('/library').prevented, true, 'the guard stays until leaving the page');
  nav.events.pageshow({persisted:true});
  assert.equal(nav.click('/library').prevented, false, 'a restored clean page can follow links again');

  const retry = launch();
  const stale = retry.click('/reading');
  retry.requests[0]({ok:false,status:503,json:async()=>({error:'Fixture failure'})});
  await stale.done;
  assert.deepEqual(retry.assignments, []);
  const resumed = retry.click('/library');
  retry.requests[1]({ok:true,json:async()=>({revision:1})});
  await resumed.done;
  assert.deepEqual(retry.assignments,[origin+'/library']);
  process.stdout.write('Draft navigation guards passed.\n');
}

verify().catch(error => { console.error(error); process.exitCode=1; });
