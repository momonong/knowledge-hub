// A same-document hash navigation does not fetch the final server-rendered answer.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const script = fs.readFileSync('knowledge_hub/static/reader.js','utf8');
async function check(state, destination) {
  const timers = [], calls = [], paragraph = {textContent:''};
  let reloads = 0;
  const location = new URL('http://127.0.0.1:8765/items/1?version=2#study-5');
  location.assign = url => {
    const next = new URL(url, location);
    if (next.origin !== location.origin || next.pathname !== location.pathname || next.search !== location.search) reloads++;
    location.href = next.href;
  };
  location.reload = () => {reloads++;};
  const form = {action:'http://127.0.0.1:8765/study/5/reconcile'};
  const pending = {dataset:{operationState:'pending'},querySelector:s=>s==='form'?form:paragraph};
  vm.runInNewContext(script,{
    document:{querySelector:s=>s==='[data-operation-state="pending"]'?pending:null,querySelectorAll:()=>[]},
    location,history:{replaceState:(_,__,url)=>{location.href=new URL(url,location).href;}},
    URL,URLSearchParams,FormData:class extends URLSearchParams {
      constructor() {super({csrf:'fixture',inspect_unknown:'1'});}
    },
    fetch:async(url,options)=>{calls.push([url,options.method,options.body.toString()]);return {ok:true,json:async()=>({state,location:destination})};},
    setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){}
  });
  await timers.shift()();
  assert.deepEqual(calls,[[form.action,'POST','csrf=fixture']],'background polling must not request manual inspection');
  if (state==='completed'||state==='failed') {
    assert.equal(reloads,1,`${state} must fetch final content even when only the hash changes`);
    assert.equal(location.href,new URL(destination,location).href);
  } else {
    assert.equal(reloads,0);
    assert.equal(timers.length,state==='pending'?1:0,'UNKNOWN stops automatic polling');
  }
}
(async()=>{
  for(const state of ['completed','failed']) {
    await check(state,'/items/1?version=2#study-5');
    await check(state,'/items/1?version=2#study-6');
    await check(state,'/items/2?version=3#study-6');
  }
  await check('unknown','/items/1?version=2#study-5');
  await check('pending','/items/1?version=2#study-5');
  process.stdout.write('Original operation result navigation passed.\n');
})().catch(error=>{console.error(error);process.exitCode=1;});
