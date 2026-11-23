'use strict';
const assert = require('node:assert/strict');
const key = name => name.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase());

class Element {
  constructor(tag = '') {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.named = {};
    this.attributes = {};
    this.style = {};
    this.hidden = false;
    this.value = '';
    this.textContent = '';
  }
  append(child) {child.parentElement = this; this.children.push(child);}
  prepend(child) {child.parentElement = this; this.children.unshift(child);}
  replaceChildren(...children) {this.children = []; children.forEach(child => this.append(child));}
  get childElementCount() {return this.children.length;}
  addEventListener(name, fn) {(this.listeners[name] ||= []).push(fn);}
  dispatchEvent(event) {
    for (const fn of this.listeners[event.type] || []) fn(event);
    return !event.defaultPrevented;
  }
  click() {
    const event = new Event('click', {cancelable: true});
    Object.defineProperty(event, 'currentTarget', {value:this});
    this.dispatchEvent(event);
    return event.defaultPrevented;
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
    if (name.startsWith('data-')) this.dataset[key(name)] = value;
  }
  removeAttribute(name) {delete this.attributes[name];}
  toggleAttribute(name, state) {if (state) this.setAttribute(name, ''); else this.removeAttribute(name);}
  getAttribute(name) {return this.attributes[name];}
  hasAttribute(name) {return name in this.attributes || name.startsWith('data-') && key(name) in this.dataset;}
  matches(selector) {
    if (selector.includes('source-paragraph[data-anchor]')) return this.tag === 'p' && this.className === 'prose';
    if (selector.startsWith('.')) return (this.className || '').split(' ').includes(selector.slice(1));
    const data = /^\[(data-[\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
    if (data) return key(data[1]) in this.dataset && (data[2] === undefined || this.dataset[key(data[1])] === data[2]);
    return this.tag === selector;
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) {return this.named[selector] || this.querySelectorAll(selector)[0] || null;}
  closest(selector) {return this.matches(selector) ? this : this.parentElement?.closest(selector) || null;}
  contains(node) {return node === this || this.children.some(child => child.contains(node));}
  focus() {}
  getBoundingClientRect() {return {left:0, top:0, right:258, bottom:60, width:258, height:60};}
}

const document = global.document = new Element();
const reader = new Element();
document.named['[data-reader]'] = reader;
document.createElement = tag => new Element(tag);
document.getElementById = () => null;
global.window = new Element();
Object.assign(window, {innerWidth:400, innerHeight:800});
global.location = {origin:'https://kh.test', hash:''};
global.CustomEvent = class extends Event {constructor(type, options) {super(type, options); this.detail = options?.detail;}};
if (!global.crypto) global.crypto = require('node:crypto').webcrypto;
global.requestAnimationFrame = callback => setImmediate(callback);
const prose = new Element('p');
prose.className = 'prose';
const paragraph = new Element();
paragraph.dataset.anchor = 'p1';
paragraph.append(prose);
reader.append(paragraph);
const selectedNode = {nodeType:3, parentElement:prose};
const selectedRange = {
  startContainer:selectedNode, endContainer:selectedNode,
  cloneRange() {return this;},
  getBoundingClientRect() {return {left:50, top:300, right:190, bottom:330};},
};
const userSelection = {rangeCount:1, isCollapsed:false, getRangeAt:() => selectedRange,
  toString:() => 'The source paragraph.'};
window.getSelection = () => userSelection;
const guide = new Element();
const workspace = new Element();
reader.dataset = {item:'4', version:'7'};
reader.named['[data-layer-conversation]'] = workspace;
reader.named['[data-layer-guide]'] = guide;
workspace.dataset = {layerPrimary:'true', layerFallback:'true'};
for (const name of ['followup','followup-status','capture-status','history','capture-list','send','cancel',
  'clear-focus','captures-refresh','recall','recall-form','initial','focus','tag-options','focus-label','focus-quote',
  'selection-feedback']) {
  workspace.named['[data-layer-' + name + ']'] = new Element();
}
const find = name => workspace.named['[data-layer-' + name + ']'];
const box = new Element('textarea');
box.value = '原本想問的草稿';
find('followup').elements = {question:box, csrf:{value:'fixture'}};
find('initial').textContent = JSON.stringify({history:[], captures:[]});
workspace.named['#layer-comment'] = new Element('input');

for (const name of ['status','content','retry','fallback','actions']) {
  guide.named['[data-layer-guide-' + name + ']'] = new Element();
}
const guideField = name => guide.named['[data-layer-guide-' + name + ']'];
const body = {anchor:'p1', href:'/items/4?version=7#p1', source_url:'https://arxiv.org/html/2601.00001v1#p1', quote:'The source paragraph.'};
const figure = {anchor:'tab.1', href:'/items/4?version=7#kh-visual-tab.1',
  source_url:'https://arxiv.org/html/2601.00001v1#tab.1', quote:'Method | Latency'};
const via = [];
reader.addEventListener('kh-open-followup', event => via.push(['open',event.detail.focusQuestion]));
reader.addEventListener('kh-reveal-evidence', event => {
  via.push(['reveal',event.detail]);
  event.preventDefault();
});

let stream;
let nextGuide = {
  item_id:4, version_id:'7', mode:'model', model:{state:'ready', route:'primary'},
  sections:[
    {role:'hook', text:'作者從延遲切入。', citations:[body]},
    {role:'mechanism', text:'系統先收集可重用的區塊。', citations:[body]},
    {role:'evidence', text:'作者比較同一個負載下的延遲。', citations:[body]},
    {role:'limits', text:'作者未評估跨區域部署。', citations:[body]},
  ],
  visual:{...figure, text:'先看 Latency 欄。', title:'一起看這張表', label:'Table 1', focus:'Method | Latency',
    citations:[figure]},
  chips:[
    {role:'mechanism', question:'作者如何判斷哪些區塊可以安全地重用？', ...body, citations:[body]},
    {role:'evidence', question:'作者的測試對照在相同的請求負載下嗎？', ...figure, citations:[figure]},
    {role:'boundary', question:'跨區域部署時哪些假設還沒有被驗證？', ...body, citations:[body]},
  ],
};
const requests = [];
global.fetch = async (url, options = {}) => {
  requests.push({url, options});
  if (url.includes('/guide?')) return {ok:true, json:async () => nextGuide};
  if (url.endsWith('/captures')) return {ok:true, json:async () => ({record:{
    id:11, kind:'tag', state:'saved', tag:'#想法', anchor:'p1', quote:'The source paragraph.',
    href:'/items/4?version=7#p1', revision:1, version_id:'7',
  }})};
  assert.equal(url, '/items/4/followup');
  return {ok:true,headers:new Headers({'content-type':'text/event-stream'}),body:new ReadableStream({
    start(controller) {stream = controller;},
  })};
};

const retryMode = process.argv[3] === 'retry';
if (retryMode) nextGuide = {
  item_id:4, version_id:'7', mode:'local_source', model:{state:'failed', route:'primary', message:'目前沒收到模型回覆。'},
  sections:[{title:'核心機制拆解', text:'本站原文導讀。', citations:[body]}],
};
require(process.argv[2]);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await tick();
  const content = guideField('content');
  if (retryMode) {
    assert.equal(content.querySelectorAll('[data-guide-chip]').length, 0);
    assert.match(guideField('status').textContent,/目前沒收到模型回覆/);
    assert.equal(guideField('retry').hidden,false);
    guideField('retry').click();
    await tick();
    assert.equal(requests[1].url,'/items/4/guide?version=7&retry=1');
    guideField('fallback').click();
    await tick();
    assert.equal(requests[2].url,'/items/4/guide?version=7&route=fallback');
    assert.equal(guideField('fallback').hidden,true);
    return;
  }
  assert.deepEqual(content.querySelectorAll('[data-guide-stage]').map(node => node.dataset.guideStage),
    ['hook','mechanism','evidence','chips']);
  assert.equal(requests.length, 1, 'The guide is fetched once on entry');
  reader.dispatchEvent(new Event('kh-visuals-ready'));
  await tick();
  assert.equal(requests.length, 1, 'Loading original figures does not dispatch a second guide');
  const visual = content.querySelector('[data-guide-visual-link]');
  assert.ok(visual.textContent.includes('Table 1'));
  assert.equal(visual.click(), true);
  assert.deepEqual(via[0], ['reveal', {anchor:'kh-visual-tab.1',quote:'Method | Latency',href:figure.href}]);
  const chips = content.querySelectorAll('[data-guide-chip]');
  assert.equal(chips.length, 3);
  chips[1].click();
  chips[1].click();
  chips[0].click();
  await tick();
  assert.equal(requests.length, 2, 'A rapid second chip or click does not duplicate a turn');
  assert.deepEqual(via[1], ['open',false]);
  const payload = Object.fromEntries(requests[1].options.body);
  assert.equal(payload.version_id, '7');
  assert.equal(payload.anchor, 'tab.1');
  assert.equal(payload.quote, figure.quote);
  assert.equal(payload.question, nextGuide.chips[1].question);
  assert.equal(box.value, '原本想問的草稿');
  stream.enqueue(new TextEncoder().encode('event: done\ndata: {}\n\n'));
  stream.close();
  await tick();
  assert.equal(box.value, '原本想問的草稿');

  document.dispatchEvent(new Event('selectionchange'));
  await tick();
  const toolbar = reader.querySelector('.layer-selection-tools');
  assert.equal(toolbar.hidden,false);
  assert.equal(toolbar.style.top,'232px');
  userSelection.isCollapsed = true;
  document.dispatchEvent(new Event('selectionchange'));
  const note = toolbar.querySelectorAll('button').find(control => control.textContent === '想法');
  note.click();
  await tick();
  const saved = Object.fromEntries(requests.at(-1).options.body);
  assert.equal(requests.at(-1).url,'/items/4/captures');
  assert.equal(saved.action,'tag');
  assert.equal(saved.anchor,'p1');
  assert.equal(saved.quote,'The source paragraph.');
  assert.equal(toolbar.hidden,true);
  assert.match(reader.querySelector('.layer-selection-receipt').textContent,/已保存/);
})().catch(error => {console.error(error);process.exitCode = 1;});
