"""Product behavior for automatic local resumption; no real model calls."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults
from unittest.mock import patch

from knowledge_hub.store import KnowledgeStore
from knowledge_hub.study import StudyError, StudyService
from checks.test_study import entry, FakeProvider


class ReadingV1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'hub.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.item, self.version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2601.00001', 'Memory retrieval and consolidation',
            'Memory retrieval consolidates past episodes before answering a question.'))
        self.other, self.other_version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2401.00001', 'Memory retrieval baseline',
            'Memory retrieval uses similarity search to return stored observations.'))

    def state(self, store=None):
        from knowledge_hub import reading
        return reading.ReadingState(store or self.store)

    def request(self, path, fields=None, *, accept=None, **app_options):
        from knowledge_hub.web import create_app
        env = {}; setup_testing_defaults(env)
        parsed = urlsplit(path)
        env.update(PATH_INFO=parsed.path, QUERY_STRING=parsed.query, HTTP_HOST='127.0.0.1:8765')
        if accept:
            env['HTTP_ACCEPT']=accept
        if fields is not None:
            raw = urlencode({'csrf': self.store.get_csrf_token(), **fields}).encode()
            env.update(REQUEST_METHOD='POST', HTTP_ORIGIN='http://127.0.0.1:8765',
                       CONTENT_TYPE='application/x-www-form-urlencoded', CONTENT_LENGTH=str(len(raw)))
            env['wsgi.input'] = io.BytesIO(raw)
        result = {}
        def start(status, headers):
            result.update(status=int(status.split()[0]), headers=dict(headers))
        result['body'] = b''.join(create_app(self.db, **app_options)(env, start)).decode()
        return result

    def test_no_background_can_teach_focal_evidence_without_forced_unrelated_sources(self):
        solo, version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2601.00009','Photosynthesis spectral kinetics',
            'Photosynthesis spectral kinetics measures chlorophyll absorption.'))
        provider = FakeProvider()
        service = StudyService(self.store, provider)
        result = service.generate(solo, version, 'solo-lesson', 'lesson', '理解本篇')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(provider.calls[0]['sources']), 1)
        self.assertIn('沒有相關背景', provider.calls[0]['limitations'])

    def test_budget_status_disables_new_generation_after_limit_or_other_article_unknown(self):
        provider = FakeProvider(budget=1)
        service = StudyService(self.store,provider)
        service.generate(self.item,self.version,'last-slot','qa','問題')
        response = self.request(f'/items/{self.other}', study_provider=provider)
        self.assertIn('本次驗收額度已用完',response['body'])
        self.assertIn('disabled aria-disabled="true">開始教我', response['body'])

    def test_lesson_receipt_opens_at_the_same_anchor_before_and_after_recovery(self):
        provider = FakeProvider(mode='timeout')
        fields = {'version_id': self.version, 'request_id': 'recover-interrupted'}
        options = {'study_provider': provider, 'accept': 'application/json'}
        submitted = self.request(f'/items/{self.item}/teach', fields, **options)
        location = json.loads(submitted['body'])['location']
        receipt = StudyService(self.store, provider).records(self.item)[0]
        self.assertEqual(receipt['state'], 'unknown')
        self.assertEqual(self.request(f'/items/{self.item}/teach', fields, **options)['status'], 409)
        reader = self.request(location, study_provider=provider)['body']
        self.assertEqual(reader.count(f'id="{urlsplit(location).fragment}"'), 1)
        self.assertIn(f'action="/study/{receipt["id"]}/reconcile"', reader)
        self.assertIn(f'href="{location}"', self.request('/reading', study_provider=provider)['body'])
        self.assertIn(f'href="{location}"', self.request('/', study_provider=provider)['body'])
        other = self.request(f'/items/{self.other}', study_provider=provider)['body']
        self.assertIn(f'href="{location}">回到原工作</a>', other)

        with patch.object(provider, 'poll', wraps=provider.poll) as lookup:
            for _ in range(2):
                result = self.request(f'/study/{receipt["id"]}/reconcile', {'inspect_unknown': '1'}, **options)
                self.assertEqual(json.loads(result['body']), {'state': 'completed', 'location': location})
            lookup.assert_called_once_with(receipt['operation_id'])
        self.assertIn('data-study-state="completed"', self.request(location, study_provider=provider)['body'])
        cached = self.request(f'/items/{self.item}/teach', fields, **options)
        self.assertEqual(cached['status'], 200)
        self.assertEqual(json.loads(cached['body'])['location'], location)
        save = {'version_id': self.version, 'record_id': receipt['id'], 'request_id': 'recovered-progress',
                'understanding': '私人理解：用來源找回原紀錄', 'questions': '下一次如何核對來源？'}
        for _ in range(2):
            saved = json.loads(self.request(f'/items/{self.item}/progress', save, **options)['body'])
        history = self.request('/reading', study_provider=provider)['body']
        self.assertIn(f'href="{saved["location"]}"', history)
        reopened = StudyService(KnowledgeStore(self.db), provider)
        self.assertEqual([(p['record_id'], p['understanding']) for p in reopened.progress(self.item, self.version)],
                         [(receipt['id'], save['understanding'])])
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn(save['understanding'], json.dumps(provider.calls, ensure_ascii=False))

    def test_pending_receipt_and_private_bookmark_both_reopen_in_the_original_version(self):
        provider = FakeProvider(mode='pending')
        self.state().save(self.item, self.version, 0, {'anchor':'p1', 'tab':'original', 'reflection':'原來的私人草稿'})
        response = self.request(f'/items/{self.item}/teach', {'version_id':self.version, 'request_id':'pending-on-old-edition'},
                                study_provider=provider, accept='application/json')
        location = json.loads(response['body'])['location']
        self.store.upsert_parsed_entry({**entry('arxiv:2601.00001','New edition','A revised publication.'),'label':'v2'})
        history = self.request('/reading', study_provider=provider)['body']
        self.assertIn(f'href="/items/{self.item}?version={self.version}#p1"', history)
        self.assertIn(f'href="{location}"', history)
        self.assertIn('原來的私人草稿', history)
        self.assertEqual(history.count('<article>'), 1)
        body = self.request(location, study_provider=provider)['body']
        self.assertIn(f'id="{urlsplit(location).fragment}"', body)
        self.assertIn('name="inspect_unknown" value="1"', body)
        self.assertEqual(len(provider.calls), 1)

    def test_source_only_question_returns_to_its_answer_without_a_draft_or_model(self):
        prompt = 'How does memory retrieval use stored episodes?'
        reply = self.request(f'/items/{self.item}/source-reading', {'version_id':self.version,
                             'request_id':'only-source-turn','prompt':prompt}, accept='application/json')
        self.assertEqual(reply['status'], 200)
        location = json.loads(reply['body'])['location']
        history = self.request('/reading')['body']
        self.assertIn(f'href="{location}"', history)
        self.assertIn(prompt, self.request(location)['body'])

    def test_failed_teaching_receipt_stays_readable_without_polling_or_resubmission(self):
        provider = FakeProvider(mode='bad_citation')
        fields = {'version_id': self.version, 'request_id':'terminal-citations'}
        response = self.request(f'/items/{self.item}/teach', fields, study_provider=provider)
        self.assertEqual(response['status'], 303)
        location = response['headers']['Location']
        self.assertIn(f'href="{location}"', self.request('/reading', study_provider=provider)['body'])
        body = self.request(location, study_provider=provider)['body']
        self.assertIn(f'id="{urlsplit(location).fragment}" data-study-state="failed"', body)
        self.assertIn('data-study-diagnostic', body)
        self.assertNotIn('查詢原工作</button>', body)
        duplicate = self.request(f'/items/{self.item}/teach', fields, study_provider=provider)
        self.assertEqual(duplicate['status'], 409)
        self.assertEqual(len(provider.calls), 1)

    def test_completed_model_reply_can_be_added_to_understanding_with_its_source(self):
        provider = FakeProvider()
        service = StudyService(self.store, provider)
        prompt = '為什麼需要合併之前的記憶？'
        reply = service.generate(self.item, self.version, 'copy-model-reply', 'qa', prompt)
        self.assertEqual(reply['state'], 'completed')
        page = self.request(f'/items/{self.item}?version={self.version}', study_provider=provider)
        self.assertEqual(page['status'], 200)
        turn = page['body'].split(f'<article id="study-{reply["id"]}"', 1)[1].split('</article>', 1)[0]
        self.assertIn('data-copy-source="reflection"', turn)
        note = turn.split('<span hidden data-source-note>', 1)[1].split('</span>', 1)[0]
        self.assertIn(prompt, note)
        section = reply['answer']['sections'][0]
        self.assertIn(section['text'], note)
        self.assertIn(f'〔原文 {self.item}/{self.version} #p1〕', note)
        self.assertEqual(len(provider.calls), 1)

    def test_fulltext_fetch_obeys_selected_old_version(self):
        old = entry('arxiv:2601.00007','Old selected source','Original abstract')
        old['scope'] = 'abstract'
        item, selected, _ = self.store.upsert_parsed_entry(old)
        newer = {**old,'label':'v2','content_hash':'b'*64,'source_url':'https://arxiv.org/abs/2601.00007v2'}
        self.store.upsert_parsed_entry(newer)
        fetched=[]
        def fetch(url):
            fetched.append(url)
            raise OSError('Unavailable')
        response=self.request(f'/items/{item}/fetch',{'version_id':selected},fetch=fetch)
        self.assertEqual(fetched,['https://arxiv.org/html/2601.00007v1',
                                  'https://arxiv.org/pdf/2601.00007v1'])
        self.assertIn('version='+str(selected),response['headers']['Location'])

    def test_submitted_question_clears_only_matching_draft_and_preserves_other_tab(self):
        from knowledge_hub.reading import ReadingState
        state=ReadingState(self.store)
        state.save(self.item,self.version,0,{'question_draft':'已送問題','reflection':'PRIVATE'})
        provider=FakeProvider()
        response=self.request(f'/items/{self.item}/study',{'version_id':self.version,'request_id':'clear-draft',
                              'mode':'qa','prompt':'已送問題'},study_provider=provider)
        self.assertEqual(response['status'],303)
        self.assertEqual(state.get(self.item,self.version)['question_draft'],'')
        self.assertEqual(state.get(self.item,self.version)['reflection'],'PRIVATE')
        current=state.get(self.item,self.version)
        state.save(self.item,self.version,current['revision'],{'question_draft':'另一分頁的新問題'})
        self.request(f'/items/{self.item}/study',{'version_id':self.version,'request_id':'keep-draft',
                      'mode':'qa','prompt':'舊問題'},study_provider=provider)
        self.assertEqual(state.get(self.item,self.version)['question_draft'],'另一分頁的新問題')

    def test_pending_poll_returns_original_receipt_without_new_dispatch(self):
        provider=FakeProvider('pending')
        row=StudyService(self.store,provider).generate(self.item,self.version,'pending-ui','qa','原問題')
        response=self.request(f'/study/{row["id"]}/reconcile',{},accept='application/json',study_provider=provider)
        self.assertEqual(response['status'],200)
        result=json.loads(response['body'])
        self.assertEqual(result['state'],'completed')
        self.assertIn('#study-'+str(row['id']),result['location'])
        self.assertEqual(len(provider.calls),1)

    def test_reading_endpoint_persists_private_draft_and_exports_locally(self):
        response = self.request(f'/items/{self.item}/reading', {'version_id': self.version,
                                'revision': 0, 'reflection': '私人理解', 'tab': 'notes'})
        self.assertEqual(response['status'], 200)
        saved = json.loads(response['body'])
        self.assertEqual(saved['revision'], 1)
        exported = self.store.export_data()
        self.assertEqual(exported['reading_sessions'][0]['reflection'], '私人理解')
        stale = self.request(f'/items/{self.item}/reading', {'version_id': self.version,
                             'revision': 0, 'reflection': 'stale'})
        self.assertEqual(stale['status'], 409)

    def test_reader_starts_without_goal_form_and_keeps_original_anchors(self):
        response = self.request(f'/items/{self.item}?version={self.version}')
        self.assertEqual(response['status'], 200)
        page = response['body']
        self.assertIn('data-reader', page)
        self.assertIn('開始教我', page)
        self.assertNotIn('這次想學到什麼', page)
        self.assertIn('id="p1"', page)
        self.assertIn('data-draft="reflection"', page)
        self.assertIn('script-src \'self\'', response['headers']['Content-Security-Policy'])

    def test_daily_home_uses_real_freshness_and_does_not_require_setup(self):
        with patch('knowledge_hub.discovery.rank', side_effect=AssertionError('Home must read the published cache')):
            response = self.request('/')
        self.assertEqual(response['status'], 200)
        self.assertIn('data-daily', response['body'])
        self.assertIn('今日選文', response['body'])
        self.assertNotIn('興趣關鍵字設定', response['body'])
        self.assertIn('/manage', response['body'])

    def test_drafts_and_position_survive_restart_without_claiming_understanding(self):
        state = self.state()
        before = state.get(self.item, self.version)
        self.assertEqual(before['revision'], 0)
        saved = state.save(self.item, self.version, 0, {
            'tab': 'original', 'anchor': 'p1', 'question_draft': '還沒送出的問題',
            'reflection': 'PRIVATE_DRAFT', 'open_questions': '仍不確定更新何時發生'})
        reopened = self.state(KnowledgeStore(self.db)).get(self.item, self.version)
        self.assertEqual(reopened, saved)
        self.assertEqual(reopened['reflection'], 'PRIVATE_DRAFT')
        self.assertEqual(StudyService(self.store).progress(self.item, self.version), [])
        self.assertEqual(self.state().recent()[0]['version_id'], str(self.version))

    def test_stale_browser_cannot_overwrite_newer_draft(self):
        state = self.state()
        first = state.save(self.item, self.version, 0, {'reflection': '新內容'})
        with self.assertRaises(StudyError) as caught:
            state.save(self.item, self.version, 0, {'reflection': '舊內容'})
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(state.get(self.item, self.version), first)

    def test_state_is_version_bound_and_rejects_arbitrary_anchors(self):
        state = self.state()
        for version, fields in [(self.other_version, {'reflection': 'wrong article'}),
                                (self.version, {'anchor': 'javascript:alert(1)'}),
                                (self.version, {'tab': 'untrusted'}),
                                (self.version, {'reflection': 'a' * 8001})]:
            with self.assertRaises(StudyError):
                state.save(self.item, version, 0, fields)
        self.assertEqual(state.get(self.item, self.version)['revision'], 0)

    def test_private_drafts_are_excluded_from_generation_and_summary_is_reviewable(self):
        state = self.state()
        state.save(self.item, self.version, 0, {'reflection': 'PRIVATE_DRAFT',
                                               'question_draft': 'PRIVATE_UNSENT'})
        provider = FakeProvider()
        service = StudyService(self.store, provider)
        lesson = service.generate(self.item, self.version, 'v1-lesson', 'lesson', '說明機制')
        self.assertNotIn('PRIVATE_', json.dumps(provider.calls, ensure_ascii=False))
        summary = state.suggest_summary(self.item, self.version)
        self.assertEqual(summary['record_id'], lesson['id'])
        self.assertTrue(summary['understanding'])
        self.assertEqual(service.progress(self.item, self.version), [])
        self.assertIn('PRIVATE_DRAFT', state.get(self.item, self.version)['reflection'])

    def test_unknown_stops_other_new_operations_in_same_session(self):
        provider = FakeProvider('timeout')
        service = StudyService(self.store, provider)
        first = service.generate(self.item, self.version, 'v1-unknown', 'qa', '問題')
        self.assertEqual(first['state'], 'unknown')
        with self.assertRaises(StudyError) as caught:
            service.generate(self.other, self.other_version, 'v1-new', 'qa', '另一篇')
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(len(provider.calls), 1)

    def test_unknown_cannot_be_bypassed_by_changing_session(self):
        provider = FakeProvider('timeout')
        provider.binding = lambda: {'session_id':'original-session','max_operations':3}
        service = StudyService(self.store,provider)
        receipt = service.generate(self.item,self.version,'unknown-original','qa','問題')
        provider.binding = lambda: {'session_id':'replacement-session','max_operations':3}
        self.assertFalse(service.availability()['available'])
        with self.assertRaises(StudyError):
            service.generate(self.other,self.other_version,'bypass-attempt','qa','另一篇')
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(service.records(self.item)[0]['operation_id'],receipt['operation_id'])

    def test_failed_refresh_retains_last_successful_timestamp(self):
        with patch('knowledge_hub.store.now_iso',return_value='2026-11-01T00:00:00Z'):
            self.store.update_source_state('arxiv','updated',None)
            self.store.update_source_state('pytorch','updated',None)
        with patch('knowledge_hub.store.now_iso',return_value='2026-11-16T00:00:00Z'):
            self.store.update_source_state('arxiv','failed','temporarily unavailable')
        source=self.store.get_sources()['arxiv']
        self.assertEqual(source['last_success_at'],'2026-11-01T00:00:00Z')
        self.assertEqual(source['last_fetched_at'],'2026-11-16T00:00:00Z')
        page=self.request('/')['body']
        self.assertIn('上次成功讀取來源：2026-11-01',page)
        self.assertIn('部分來源讀取失敗',page)

    def test_followup_keeps_lesson_and_intermediate_question_context(self):
        provider = FakeProvider()
        service = StudyService(self.store, provider)
        lesson = service.generate(self.item, self.version, 'multi-1', 'lesson', '理解這篇')
        question = service.generate(self.item, self.version, 'multi-2', 'qa', '用例子解釋', lesson['id'])
        service.generate(self.item, self.version, 'multi-3', 'qa', '那更新時呢？', question['id'])
        prompts = [turn['prompt'] for turn in provider.calls[-1]['conversation']]
        self.assertEqual(prompts, ['理解這篇', '用例子解釋'])

    def test_daily_refresh_runs_once_per_local_day_and_keeps_failure_visible(self):
        from knowledge_hub.daily import DailyRefresh
        daily = DailyRefresh(self.store)
        self.assertTrue(daily.due())
        calls = []
        def sync():
            calls.append('refresh')
            self.store.update_source_state('arxiv','failed','upstream unavailable')
        result = daily.run(sync)
        self.assertEqual(result['state'], 'partial')
        self.assertFalse(daily.due())
        self.assertEqual(daily.run(sync)['state'], 'partial')
        self.assertEqual(calls, ['refresh'])
        self.assertTrue(self.store.get_item(self.item))


if __name__ == '__main__':
    unittest.main()
