// Execute the actual browser script with a controlled, unresolved save request.
// This models leaving the page while the pre-submit autosave is still in flight.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const events = {}, documentEvents = {}, formEvents = {};
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
vm.runInNewContext(fs.readFileSync('knowledge_hub/static/reader.js','utf8'),{
  document,window:{addEventListener(name,fn){events[name]=fn;}},
  location:{hash:'',href:'http://127.0.0.1:8765/items/1',origin:'http://127.0.0.1:8765',pathname:'/items/1',search:''},
  fetch:()=>new Promise(()=>{}), URLSearchParams,URL,Number,
  setTimeout(){return 1;},clearTimeout(){},requestAnimationFrame(){}
});
function protectedUnload(){let prevented=false;events.beforeunload({preventDefault(){prevented=true;}});return prevented;}
assert.equal(protectedUnload(),true,'dirty state must warn before leaving');
formEvents.submit({preventDefault(){}});
assert.equal(status.textContent,'正在保存到本機…');
assert.equal(protectedUnload(),true,'a form submission must not suppress the unfinished-save warning');
let linkPrevented=false;
documentEvents.click({target:{closest:()=>({href:'http://127.0.0.1:8765/',target:''})},
  preventDefault(){linkPrevented=true;}});
assert.equal(linkPrevented,true,'navigation cannot abandon the pre-submit save');
process.stdout.write('Draft navigation guards passed.\n');
