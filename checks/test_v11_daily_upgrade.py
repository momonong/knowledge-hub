"""An existing briefing can adopt verified fixed-version originals."""
from collections import Counter
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub import briefs
from knowledge_hub.daily import DailyRefresh
from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.sources import compute_content_hash, parse_arxiv_feed, parse_arxiv_html
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from test_initial_library import seed


PAPER = '2611.02023'
HTML = f'https://arxiv.org/html/{PAPER}v1'
ABSTRACT = f'https://arxiv.org/abs/{PAPER}v1'
RESULT = '作者報告 Recall 從 61% 提升至 78%。'
OLD = '摘要未交代測量結果。'
FULL = (b'<article><h1>Retrieval systems</h1><h2 id="method">2 Method</h2>'
        b'<p id="p1">We propose a retrieval strategy to rerank candidates for long-term memory.</p>'
        b'<h2 id="results">3 Results</h2><p id="p2">Recall increases from 61% to 78% against a BM25 baseline.</p>'
        b'<figure id="T1" class="ltx_table"><figcaption>Table 1: Recall on our benchmark.</figcaption>'
        b'<table><tr><th>Method</th><th>Recall</th></tr><tr><td>Ours</td><td>78%</td></tr></table>'
        b'</figure></article>')


def tier(metrics=OLD):
    return {'l1': {'change':'作者改用重排。','highlight':'先篩候選。','relevance':'對應檢索。'},
            'l2': {'mechanism':'先檢索再重排。','metrics':metrics,
                   'comparison':'比較 BM25。','limits':'限於作者案例。'}}


class DailyUpgrade(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'reading.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.cache = DailyRefresh(self.store)
        self.today = self.cache.day()
        raw = (f'<feed><entry><id>{ABSTRACT}</id><title>Retrieval systems</title>'
               f'<summary>We propose a retrieval strategy to rerank candidates for long-term memory.</summary>'
               f'<published>{self.today}T00:00:00Z</published></entry></feed>').encode()
        abstract, = parse_arxiv_feed(raw)
        item_id, version_id, _ = self.store.upsert_parsed_entry(abstract)
        self.item_id,self.version_id = item_id,version_id
        self.pick = {'item':{'id':item_id,'key':abstract['key'],'title':abstract['title'],'source_name':'arxiv'},
                     'version':{'id':version_id,**abstract}, 'category':'AI Agents、記憶與檢索',
                     'spectrum_reason':'與檢索重排相關。','reasons':[{'title':'PRIVATE_READING'}],
                     'brief':{**tier(),'state':'ready','attempts':1,'error':''},
                     'brief_format':briefs.FORMAT_VERSION}
        self.cache.run(lambda: [self.pick],day=self.today)
        self.requests = []

    def fetch(self, url):
        self.requests.append(url)
        if url == HTML:
            return FULL
        raise OSError('Test has no real network')

    def request(self, app, path, fields=None):
        env = {}
        setup_testing_defaults(env)
        env.update(HTTP_HOST='localhost',PATH_INFO=path)
        if fields is not None:
            body = urlencode({'csrf':self.store.get_csrf_token(),**fields}).encode()
            env.update(REQUEST_METHOD='POST',CONTENT_TYPE='application/x-www-form-urlencoded',
                       CONTENT_LENGTH=str(len(body)),HTTP_ORIGIN='http://localhost')
            env['wsgi.input'] = io.BytesIO(body)
        result = {}
        def start(status, headers):
            result.update(status=int(status.split()[0]),headers=dict(headers))
        result['body'] = b''.join(app(env,start)).decode()
        return result

    def test_fetch_upgrades_brief_home_and_choices_without_replacing_the_card(self):
        self.cache.choose(self.today,self.item_id,self.version_id,'later')
        requests = []
        def generate(pick):
            requests.append(pick)
            self.assertEqual(pick['version']['source_url'],HTML)
            self.assertIn('78%',str(pick['version']['blocks']))
            self.assertNotIn('PRIVATE_READING',str(pick))
            return tier(RESULT)
        app = create_app(self.db,fetch=self.fetch,brief_generator=generate)
        self.assertIn(OLD,self.request(app,'/')['body'])
        response = self.request(app,f'/items/{self.item_id}/fetch',{'version_id':self.version_id})
        self.assertEqual(response['status'],303)
        restarted = DailyRefresh(KnowledgeStore(self.db))
        pick, = restarted.selection(self.today)
        self.assertEqual(pick['brief']['l2']['metrics'],RESULT)
        self.assertEqual(pick['brief']['mode'],'model')
        self.assertIn('T1',str(requests[0]['brief_panels']))
        self.assertNotIn('brief_panels',pick)
        self.assertEqual(pick['version']['label'],'v1')
        self.assertNotEqual(pick['version']['id'],self.version_id)
        self.assertEqual(pick['version']['published_at'],self.pick['version']['published_at'])
        self.assertEqual((pick['item']['id'],pick['choice']),(self.item_id,'later'))
        self.assertEqual(restarted.latest_day(),self.today)
        self.assertIn(RESULT,self.request(app,'/')['body'])
        self.assertNotIn(OLD,self.request(app,'/')['body'])
        restarted.choose(self.today,self.item_id,pick['version']['id'],'skip')
        self.assertEqual(restarted.selection(self.today)[0]['choice'],'skip')
        self.assertEqual(self.store.get_item(self.item_id)['status'],'unread')
        restarted.choose(self.today,self.item_id,self.version_id,'undo')
        self.assertEqual(len(requests),1)

    def test_failed_full_generation_preserves_old_brief_and_can_retry_after_restart(self):
        attempts = []
        def temporary_failure(pick):
            attempts.append(pick['version']['id'])
            if len(attempts) == 1:
                raise TimeoutError()
            return tier(RESULT)
        first = create_app(self.db,fetch=self.fetch,brief_generator=temporary_failure)
        self.request(first,f'/items/{self.item_id}/fetch',{'version_id':self.version_id})
        self.assertEqual(len(attempts),1)
        self.assertEqual(self.cache.selection(self.today)[0]['brief']['l2']['metrics'],OLD)
        second = create_app(self.db,fetch=self.fetch,brief_generator=temporary_failure)
        with patch('knowledge_hub.feeds.collect'):
            second.refresh_daily_sources(prefetch=False)
            second.refresh_daily_sources(prefetch=False)
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.cache.selection(self.today)[0]['brief']['l2']['metrics'],RESULT)

    def test_configured_model_rebuilds_saved_local_version_without_reranking(self):
        generated = []
        def generate(pick):
            generated.append(pick['version']['id'])
            return tier('模型補充的摘要結論')
        app = create_app(self.db,fetch=self.fetch,brief_generator=generate)
        with patch('knowledge_hub.feeds.collect'), patch('knowledge_hub.discovery.rank',side_effect=AssertionError('reranked')):
            app.refresh_daily_sources(prefetch=False)
            app.refresh_daily_sources(prefetch=False)
        self.assertEqual(generated,[self.version_id])
        self.assertEqual(self.cache.selection(self.today)[0]['brief']['mode'],'model')
        self.assertEqual(self.requests,[])

    def test_exhausted_background_attempts_can_be_repaired_by_manual_refresh(self):
        self.store.upsert_parsed_entry(parse_arxiv_html(FULL,HTML))
        calls = []
        def generate(pick):
            calls.append(pick['version']['scope'])
            if len(calls) <= 2:
                raise TimeoutError()
            return tier(RESULT)
        app = create_app(self.db,fetch=self.fetch,brief_generator=generate)
        with patch('knowledge_hub.feeds.collect'):
            for _ in range(3):
                app.refresh_daily_sources(prefetch=False)
            self.assertEqual(calls,['full','full'])
            self.assertEqual(self.cache.selection(self.today)[0]['brief']['l2']['metrics'],OLD)
            response = self.request(app,'/sync',{})
            self.assertEqual(response['status'],303)
        self.assertEqual(calls,['full','full','full'])
        self.assertEqual(self.cache.selection(self.today)[0]['brief']['l2']['metrics'],RESULT)

    def test_running_upgrade_is_not_sent_again_by_another_refresh(self):
        self.store.upsert_parsed_entry(parse_arxiv_html(FULL,HTML))
        calls = []
        def duplicate(_):
            calls.append('duplicate upgrade')
            return tier()
        other = create_app(self.db,fetch=self.fetch,brief_generator=duplicate)
        def generate(pick):
            calls.append(pick['version']['scope'])
            other.refresh_daily_sources(prefetch=False)
            return tier(RESULT)
        app = create_app(self.db,fetch=self.fetch,brief_generator=generate)
        with patch('knowledge_hub.feeds.collect'):
            app.refresh_daily_sources(prefetch=False)
        self.assertEqual(calls,['full'])
        self.assertEqual(self.cache.selection(self.today)[0]['brief']['l2']['metrics'],RESULT)

    def test_higher_edition_and_attached_same_edition_do_not_reach_the_model(self):
        self.store.upsert_parsed_entry(parse_arxiv_html(FULL,HTML[:-1]+'2'))
        lib = KnowledgeLibrary(self.store)
        catalog,graph,starter = seed()
        catalog['sources'][0]['primary_url'] = HTML
        lib.import_seed(catalog,graph,starter)
        lib.add_document('sample-rag',b'PRIVATE_ATTACHMENT',media_type='text/plain',source_url=HTML,
                         version_label='v1',scope='full',
                         blocks=[{'anchor':'p1','text':'PRIVATE_ATTACHMENT','source_url':HTML+'#p1'}])
        calls = []
        def generate(pick):
            calls.append(pick)
            return tier()
        app = create_app(self.db,fetch=self.fetch,brief_generator=generate)
        with patch('knowledge_hub.feeds.collect'):
            app.refresh_daily_sources(prefetch=False)
        self.assertEqual([p['version']['id'] for p in calls],[self.version_id])
        self.assertEqual(self.cache.selection(self.today)[0]['version']['id'],self.version_id)
        self.assertNotIn('PRIVATE_ATTACHMENT',json.dumps(calls))

    def test_background_fills_missing_metric_from_the_same_edition_pdf(self):
        pdf_url = HTML.replace('/html/','/pdf/')
        body = 'We propose a retrieval strategy. Recall increases from 61% to 78% against a BM25 baseline.'
        blocks = [{'anchor':'page-3','text':body,'source_url':pdf_url+'#page=3'}]
        title = self.pick['item']['title']
        _, pdf_id, _ = self.store.upsert_parsed_entry({
            'key':self.pick['item']['key'],'title':title,'source_name':'arxiv','label':'v1','scope':'partial',
            'source_url':pdf_url,'blocks':blocks,'content_hash':compute_content_hash(title,'partial','v1',blocks),
        })
        def generate(pick):
            self.assertEqual(pick['version']['id'],pdf_id)
            self.assertEqual(pick['version']['blocks'],blocks)
            return tier(RESULT)
        app = create_app(self.db,fetch=self.fetch,brief_generator=generate)
        with patch('knowledge_hub.feeds.collect'):
            app.refresh_daily_sources(prefetch=False)
        self.assertEqual(self.requests,[])
        pick, = self.cache.selection(self.today)
        self.assertEqual(pick['brief']['l2']['metrics'],RESULT)
        self.assertEqual((pick['version']['id'],pick['version']['scope'],pick['version']['source_url']),
                         (pdf_id,'partial',pdf_url))

    def test_recovered_feeds_append_to_existing_decks_without_touching_choices_or_positions(self):
        topics = (
            ('Long-term memory with agent tools','AI Agents、記憶與檢索'),
            ('RAG and agent memory','AI Agents、記憶與檢索'),
            ('Vision-language alignment for a camera','多模態 AI'),
            ('World models with a simulated robot','Physical AI 與世界模型'),
            ('Multi-agent memory with timestamps','AI Agents、記憶與檢索'),
            ('Agentic memory for RAG','AI Agents、記憶與檢索'),
            ('KV cache with Speculative Decoding','推論最佳化與 ML 系統'),
            ('Test-time scaling with data curation','模型能力與訓練典範'),
            ('Multimodal reasoning for color','多模態 AI'),
            ('World models for a different robot','Physical AI 與世界模型'),
        )
        for count in (1,2,3,4):
            with self.subTest(existing=count), TemporaryDirectory() as tmp:
                path = Path(tmp) / 'recovery.sqlite3'
                store = KnowledgeStore(path)
                daily = DailyRefresh(store)
                day = daily.day()

                def entry(index, label='v1'):
                    title,_ = topics[index]
                    url = f'https://arxiv.org/abs/2611.{71000+index}{label}'
                    text = f'We propose {title} and report 35% lower latency against the existing benchmark.'
                    raw = (f'<feed><entry><id>{url}</id><title>{title}</title>'
                           f'<summary>{text}</summary><published>{day}T00:00:00Z</published></entry></feed>')
                    return parse_arxiv_feed(raw.encode())[0]

                initial = []
                for i in range(count):
                    source = entry(i)
                    item_id,version_id,_ = store.upsert_parsed_entry(source)
                    initial.append({'item':{'id':item_id,'key':source['key'],'title':source['title'],'source_name':'arxiv'},
                                    'version':{**source,'id':version_id},'category':topics[i][1],
                                    'brief':{**tier(),'state':'ready'},'brief_format':briefs.FORMAT_VERSION})
                daily.run(lambda: initial)
                first = initial[0]
                daily.choose(day,first['item']['id'],first['version']['id'],'later')
                if count > 1:
                    daily.choose(day,initial[1]['item']['id'],initial[1]['version']['id'],'skip')

                def snapshot():
                    with store._connect() as conn:
                        return tuple(tuple(map(tuple,conn.execute(sql))) for sql in (
                            'SELECT rowid,* FROM daily_cards ORDER BY position',
                            'SELECT * FROM daily_choices ORDER BY item_id',
                            'SELECT * FROM daily_status_owner ORDER BY item_id'))

                before = snapshot()
                app = create_app(path,fetch=self.fetch)
                with patch('knowledge_hub.feeds.collect',side_effect=lambda *_: store.update_source_state('arxiv','failed')):
                    app.refresh_daily_sources(prefetch=False,force=True)
                self.assertEqual(snapshot(),before)

                def restored(*_):
                    for i in range(count,len(topics)):
                        store.upsert_parsed_entry(entry(i))
                    store.upsert_parsed_entry(entry(0,'v2'))  # Same article, new revision.
                    store.update_source_state('arxiv','updated')

                with patch('knowledge_hub.feeds.collect',side_effect=restored):
                    for _ in range(2):
                        app.refresh_daily_sources(prefetch=False,force=True)
                rows,choices,owner = snapshot()
                self.assertEqual(len(rows),5)
                self.assertEqual(rows[:count],before[0])
                self.assertEqual((choices,owner),before[1:])
                resolved = daily.selection(day)
                self.assertEqual([pick['version']['id'] for pick in resolved[:count]],
                                 [pick['version']['id'] for pick in initial])
                self.assertEqual(len({pick['item']['key'] for pick in resolved}),5)
                self.assertLessEqual(max(Counter(pick['category'] for pick in resolved).values()),2)


if __name__ == '__main__':
    unittest.main()
