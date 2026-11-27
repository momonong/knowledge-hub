"""The daily reading list learns locally from real product state."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from knowledge_hub.daily import DailyRefresh, run_hosted_refresh
from knowledge_hub.discovery import classify_cached_picks, make_public_domain_cache, rank
from knowledge_hub.reading import ReadingState
from knowledge_hub.sources import HF_PAPERS_API, PUBLIC_PROJECTS_URL, arxiv_abs_url, arxiv_metadata_url, parse_huggingface_papers, validate_fetch_url
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app


def feed(rows, date):
    return json.dumps([{'paper':{'id':key,'title':title,'summary':abstract,'publishedAt':date,
                                'upvotes':votes,'ai_summary':'NOT_THE_AUTHOR_ABSTRACT'}}
                       for key,title,abstract,votes in rows]).encode()


RELATED = ('2611.12345','Episode retrieval with timestamp updates',
           'We evaluate long-term memory retrieval on three benchmarks and report a 12% improvement against a timestamp-blind baseline.',1)
UNRELATED = ('2611.12346','Spectral analysis of ocean tides',
             'We study wave height observations at two coastal stations and evaluate model accuracy on 5 datasets.',99999)


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/'reading.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.now = datetime.now(timezone.utc)
        raw = feed([('2601.00001','Old agent memory article',
                      'The method uses long-term memory and retrieval to answer questions about changed facts.',0)],
                    '2026-01-01T00:00:00Z')
        self.item,self.version,_ = self.store.upsert_parsed_entry(parse_huggingface_papers(raw)[0])
        ReadingState(self.store).save(self.item,self.version,0,{'open_questions':'舊事實的記憶更新如何影響檢索？ PRIVATE_SENTINEL'})
        self.assertEqual(self.store.get_interests(),[])

    def test_rank_links_questions_and_prior_reading_with_actual_evidence(self):
        projects = [{'name':'Research Reader','topics':['retrieval'],'active_at':self.now.isoformat()}]
        extra = parse_huggingface_papers(feed([UNRELATED,RELATED],self.now.isoformat()))
        picks = rank(self.store,projects,extra=extra,now=self.now)
        self.assertEqual([p['item']['key'] for p in picks],['arxiv:2611.12345'])
        self.assertEqual([p['kind'] for p in picks[0]['reasons']],['question','reading'])
        self.assertEqual(picks[0]['reasons'][0]['href'],f'/items/{self.item}?version={self.version}')
        self.assertIn('12% improvement',picks[0]['evidence'])
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(picks))
        self.assertNotIn('NOT_THE_AUTHOR_ABSTRACT',json.dumps(picks))

    def test_daily_refresh_only_calls_fixed_public_feeds_and_keeps_arxiv_identity(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if url == PUBLIC_PROJECTS_URL:
                project = {'name':'public-reader','full_name':'momonong/public-reader','private':False,
                           'archived':False,'fork':False,'pushed_at':self.now.isoformat(),
                           'description':'A long-term memory retrieval tool'}
                return json.dumps([project,{**project,'name':'PRIVATE_SENTINEL','full_name':'momonong/PRIVATE_SENTINEL',
                                            'private':True}]).encode()
            if url.startswith(HF_PAPERS_API):
                return feed([RELATED,UNRELATED],self.now.isoformat())
            raise OSError('Fixture only provides Hugging Face metadata')
        app = create_app(self.db,fetch=fetch,public_projects=True)
        def request(path, fields=None):
            env={};setup_testing_defaults(env)
            env.update(PATH_INFO=path,HTTP_HOST='localhost')
            if fields is not None:
                body=urlencode({'csrf':self.store.get_csrf_token(),**fields}).encode()
                env.update(REQUEST_METHOD='POST',CONTENT_TYPE='application/x-www-form-urlencoded',CONTENT_LENGTH=str(len(body)),
                           HTTP_ORIGIN='http://localhost')
                env['wsgi.input']=io.BytesIO(body)
            response={}
            def start(status, headers): response['status']=int(status.split()[0]);response['headers']=dict(headers)
            response['body']=b''.join(app(env,start)).decode()
            return response
        self.assertEqual(request('/sync/daily',{})['status'],200)
        feed_calls = list(calls)
        self.assertNotIn(PUBLIC_PROJECTS_URL, feed_calls)
        # The hosted worker uses the same durable daily gate as the reader.
        self.assertEqual(app.refresh_daily_sources()['state'],'partial')
        self.assertEqual(calls[len(feed_calls):], [arxiv_metadata_url(RELATED[0]),arxiv_abs_url(RELATED[0])])
        self.assertTrue(all(validate_fetch_url(url)==url and 'PRIVATE_SENTINEL' not in url for url in calls))
        self.assertEqual(request('/sync/daily',{})['status'],200)
        self.assertEqual(len(calls), len(feed_calls) + 2)
        page=request('/')['body']
        self.assertIn('Episode retrieval with timestamp updates',page)
        self.assertIn('回到未解問題',page)
        self.assertIn('接上閱讀',page)
        self.assertNotIn('接上專案',page)
        self.assertNotIn('public-reader',page)
        self.assertNotIn('momonong/PRIVATE_SENTINEL',page)
        self.assertNotIn(UNRELATED[1],page)
        new_item = next(row for row in self.store.get_all_items() if row['key']=='arxiv:2611.12345')
        self.assertEqual(len(new_item['versions']),1)
        self.assertEqual(new_item['versions'][0]['label'],'hf-index')
        detail=request(f'/items/{new_item["id"]}')
        self.assertEqual(detail['status'],200)
        self.assertIn('12% improvement',detail['body'])

    def test_home_adds_domains_to_a_cached_fixed_edition_without_old_project_reasons(self):
        title = 'Action-conditioned dynamics for long horizons'
        abstract = ('We train a world model using reinforcement learning. '
                    'We evaluate the policy for robotic manipulation and autonomous driving.')
        pick_entry, = parse_huggingface_papers(feed([('2611.18591', title, abstract, 0)], self.now.isoformat()))
        item_id, version_id, _ = self.store.upsert_parsed_entry(pick_entry)
        item = self.store.get_item(item_id)
        version = self.store.select_version(item['versions'], version_id)
        old_pick = {
            'item': item, 'version': version,
            'reasons': [{'kind': 'project', 'title': 'obsolete-project', 'href': '', 'matches': ['obsolete-topic']}],
            'personalized': True,
        }
        refresh = DailyRefresh(self.store)
        refresh.run(lambda: [old_pick], day=refresh.day())
        env = {}
        setup_testing_defaults(env)
        env.update(HTTP_HOST='localhost', PATH_INFO='/')
        app = create_app(self.db, fetch=lambda url: self.fail(url), public_projects=True)
        page = b''.join(app(env, lambda status, _: self.assertEqual(status, '200 OK'))).decode()
        for domain in ('World Models', 'RL', 'Robotics', '自動駕駛'):
            self.assertIn(domain, page)
        self.assertIn('相關領域', page)
        self.assertNotIn('接上專案', page)
        self.assertNotIn('obsolete-project', page)
        self.assertEqual(refresh.selection(refresh.day())[0]['reasons'], old_pick['reasons'])

    def test_public_domain_cache_keys_off_fixed_content_and_is_owned_by_each_app(self):
        cache = make_public_domain_cache()
        date = self.now.isoformat()
        key, title = '2611.18592', 'Learning from interactions'
        old, new = (
            parse_huggingface_papers(feed([(key, title, summary, 0)], date))[0]
            for summary in ('We train a world model.', 'We study reinforcement learning.')
        )
        item_id, first_version, _ = self.store.upsert_parsed_entry(old)
        _, latest_version, _ = self.store.upsert_parsed_entry(new)
        self.assertNotEqual(first_version, latest_version)
        def classified(version):
            picks = [{'item': {'id': item_id}, 'version': {'id': version}}]
            return classify_cached_picks(self.store, picks, cache)[0]['domains']
        self.assertEqual(classified(first_version), {'World Models': 'world model'})
        self.assertEqual(classified(latest_version), {'RL': 'reinforcement learning'})
        self.assertEqual(classified(first_version), {'World Models': 'world model'})
        self.assertEqual((cache.cache_info().hits, cache.cache_info().misses), (1, 2))
        self.assertEqual(make_public_domain_cache().cache_info().currsize, 0)

    def test_allowlist_refuses_outbound_queries_containing_signal_text(self):
        with self.assertRaises(ValueError):
            validate_fetch_url(HF_PAPERS_API+'?month=2026-11&sort=trending&limit=50&p=0&q=PRIVATE_SENTINEL')

    def test_hosted_refresh_runs_without_opening_the_reader(self):
        delays, refreshed = [], []
        class Stop:
            def wait(self, seconds):
                delays.append(seconds)
                return len(delays) == 3
        hours = iter([6,7])
        run_hosted_refresh(lambda:refreshed.append(True),Stop(),now=lambda:self.now.replace(hour=next(hours)))
        self.assertEqual(delays[0], 1)
        self.assertTrue(all(0 < delay <= 600 for delay in delays[1:]))
        self.assertEqual(refreshed,[True])


if __name__ == '__main__': unittest.main()
