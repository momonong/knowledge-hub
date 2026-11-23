"""The edition UI keeps private data inert and handles a split SSE stream."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from knowledge_hub import layer_views


class Markup(HTMLParser):
    def __init__(self, raw):
        super().__init__()
        self.tags = []
        self.scripts = []
        self.json_script = False
        self.feed(raw)

    def handle_starttag(self, name, attrs):
        attrs = dict(attrs)
        self.tags.append((name,attrs))
        self.json_script = name == 'script' and attrs.get('type') == 'application/json'

    def handle_data(self, text):
        if self.json_script:
            self.scripts.append(text)


class LayerViewsTests(unittest.TestCase):
    def test_followup_keeps_legacy_draft_hooks_without_legacy_submit_listener(self):
        raw = layer_views.conversation(4,'7','fixture-csrf',{'question_draft':'舊草稿'},[],[],
                                       primary_model=False,fallback_model=True)
        markup = Markup(raw)
        fields = [attrs for name,attrs in markup.tags if name == 'textarea' and attrs.get('id') == 'followup']
        self.assertEqual(len(fields),1)
        self.assertEqual(fields[0]['data-draft'],'question_draft')
        self.assertNotIn('data-question-form',raw)
        self.assertNotIn('data-busy',raw)
        self.assertIn('id="draft-status"',raw)
        self.assertIn('action="/items/4/followup"',raw)
        self.assertIn('data-layer-fallback="true"',raw)
        self.assertIn('本站原文查讀',raw)

    def test_history_and_captures_use_inert_json_even_when_text_closes_script_tag(self):
        attack = '</script><img src=x onerror=run()>'
        raw = layer_views.conversation(1,'2','csrf',{'question_draft':attack},
                                       [{'id':1,'question':attack}],[{'id':4,'text':attack}])
        markup = Markup(raw)
        self.assertFalse(any(name=='img' for name,_ in markup.tags))
        state = json.loads(''.join(markup.scripts))
        self.assertEqual(state['history'][0]['question'],attack)
        self.assertEqual(state['captures'][0]['text'],attack)

    def test_guide_exposes_status_content_and_retry_controls(self):
        markup = Markup(layer_views.guide_panel())
        self.assertTrue(any(attrs.get('role') == 'status' for _,attrs in markup.tags))
        self.assertTrue(any('data-layer-guide-content' in attrs for _,attrs in markup.tags))
        self.assertTrue(any(name == 'button' and 'data-layer-guide-retry' in attrs and 'hidden' in attrs
                            for name,attrs in markup.tags))

    @unittest.skipUnless(shutil.which('node'),'Node is required for the SSE timing regression')
    def test_client_dispatches_complete_events_as_utf8_chunks_arrive(self):
        module = Path(layer_views.__file__).parent/'static'/'layers.js'
        script = r"""
        const assert = require('node:assert/strict');
        const {readEvents, safeHref} = require(process.argv[1]);
        const encoder = new TextEncoder();
        let source;
        const input = new ReadableStream({start(controller) { source = controller; }});
        const seen = [];
        const reading = readEvents(input, (kind,payload) => { seen.push([kind,payload]); });
        const first = encoder.encode('event: section\r\ndata: {"text":"測試"}\r\n\r\n');
        const sectionByte = first.indexOf(230);
        source.enqueue(first.slice(0,sectionByte+1));
        source.enqueue(first.slice(sectionByte+1,first.length-1));
        source.enqueue(first.slice(first.length-1));
        setImmediate(async () => {
          assert.deepEqual(seen,[['section',{text:'測試'}]]);
          source.enqueue(encoder.encode(': heartbeat\n\nevent: done\ndata: {"id":3}\n\n'));
          source.close();
          await reading;
          assert.deepEqual(seen[1],['done',{id:3}]);
          const origin = 'https://example.org';
          assert.equal(safeHref('/items/4?version=7#p1','/knowledge',origin),'/knowledge/items/4?version=7#p1');
          assert.equal(safeHref('/knowledge/items/4?version=7','/knowledge',origin),'/knowledge/items/4?version=7');
          for (const url of ['//evil.example/items/1','javascript:alert(1)','/items/1/../../outside','/items/1\\evil'])
            assert.equal(safeHref(url,'/knowledge',origin),null,url);
        });
        """
        result = subprocess.run(['node','-e',script,str(module)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless(shutil.which('node'),'Node is required for the draft timing regression')
    def test_pending_autosave_keeps_new_edits_and_conflicts_preserve_the_text(self):
        module = Path(layer_views.__file__).parent/'static'/'layers.js'
        script = r"""
        const assert = require('node:assert/strict');
        const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
        class Element {
          constructor(tag = '') {
            this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {};
            this.listeners = {}; this.named = {}; this.value = ''; this.hidden = false;
          }
          append(child) { child.parent = this; this.children.push(child); }
          prepend(child) { child.parent = this; this.children.unshift(child); }
          replaceWith(replacement) {
            const i = this.parent.children.indexOf(this);
            this.parent.children[i] = replacement; replacement.parent = this.parent;
          }
          addEventListener(name,listener) { (this.listeners[name] ||= []).push(listener); }
          trigger(name) {
            let prevented = false;
            const event = {currentTarget:this,preventDefault() {prevented = true;}};
            for (const listener of this.listeners[name] || []) listener(event);
            return prevented;
          }
          setAttribute(name,value) { this.attributes[name] = value; }
          toggleAttribute(name,selected) {
            if (selected) this.attributes[name] = ''; else delete this.attributes[name];
          }
          querySelector(selector) {
            if (Object.hasOwn(this.named,selector)) return this.named[selector];
            if (selector.split(',').map(s => s.trim()).includes(this.tag)) return this;
            return this.children.map(child => child.querySelector(selector)).find(Boolean) || null;
          }
          querySelectorAll() { return []; }
          focus() {}
        }
        const reader = new Element();
        reader.dataset = {item:'4',version:'7'};
        const workspace = new Element();
        workspace.dataset = {layerFallback:'false'};
        reader.named['[data-layer-conversation]'] = workspace;
        const source = {id:9,version_id:'7',kind:'understanding',state:'draft',turn_id:1,text:'一開始',revision:1};
        for (const name of ['followup','followup-status','capture-status','history','capture-list','send','cancel',
             'clear-focus','captures-refresh','recall','recall-form','initial']) {
          workspace.named['[data-layer-' + name + ']'] = new Element();
        }
        const find = name => workspace.named['[data-layer-' + name + ']'];
        find('followup').elements = {question:new Element('textarea'),csrf:{value:'fixture'}};
        workspace.named['#layer-comment'] = new Element('input');
        find('initial').textContent = JSON.stringify({history:[],captures:[source]});
        const document = global.document = new Element();
        document.named['[data-reader]'] = reader;
        document.createElement = tag => new Element(tag);
        document.getElementById = () => null;
        global.location = {origin:'https://example.org',hash:''};
        global.window = new Element();
        const sent = [];
        global.fetch = async (path, options) => {
          assert.equal(path,'/items/4/captures');
          return new Promise(resolve => sent.push({
            fields:Object.fromEntries(options.body), keepalive:options.keepalive,
            reply(record,status = 200) {
              resolve({ok:status === 200,status,json:async () => status === 200 ? {record} : {error:'另有更新'}});
            },
          }));
        };
        require(process.argv[1]);
        async function waitForLength(count) {
          for (let n=0; n<35 && sent.length<count; n++) await sleep(35);
          assert.equal(sent.length,count);
        }
        (async () => {
          const draft = find('capture-list').querySelector('textarea');
          draft.value = '第一個修改'; draft.trigger('input');
          await waitForLength(1);
          assert.equal(sent[0].fields.text,'第一個修改');
          assert.equal(sent[0].fields.revision,'1');
          draft.value = '較晚打的修改'; draft.trigger('input');
          sent[0].reply({...source,revision:2,text:'第一個修改'});
          await waitForLength(2);
          assert.equal(find('capture-list').querySelector('textarea'),draft);
          assert.equal(draft.value,'較晚打的修改');
          assert.equal(sent[1].fields.revision,'2');
          assert.equal(sent[1].fields.text,'較晚打的修改');
          sent[1].reply({...source,revision:3,text:'較晚打的修改'});
          await sleep(20);
          draft.value = '離開前還有修改'; draft.trigger('input');
          assert.equal(window.trigger('beforeunload'),false);
          await waitForLength(3);
          assert.equal(sent[2].keepalive,true);
          assert.equal(sent[2].fields.revision,'3');
          sent[2].reply(null,409);
          await sleep(20);
          assert.equal(draft.value,'離開前還有修改');
          assert.equal(window.trigger('beforeunload'),true);
          const controls = draft.parent.children.find(el => el.className === 'layer-actions');
          assert.equal(controls.children.find(el => el.textContent === '複製目前草稿').hidden,false);
        })().catch(error => {console.error(error);process.exitCode = 1;});
        """
        result = subprocess.run(['node','-e',script,str(module)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
