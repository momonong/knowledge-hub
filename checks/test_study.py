"""Behavioral checks for the generic learning loop; providers here are synthetic."""
import copy
import hashlib
import json
import io
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

from knowledge_hub.store import KnowledgeStore


def entry(key, title, text):
    return {'key': key, 'source_name': 'arxiv', 'title': title, 'label': 'v1',
            'scope': 'full', 'source_url': 'https://arxiv.org/html/' + key.split(':')[-1] + 'v1',
            'author': 'Synthetic', 'published_at': '2026-01-01',
            'content_hash': hashlib.sha256(text.encode()).hexdigest(),
            'blocks': [{'anchor': 'p1', 'text': text}]}


class FakeProvider:
    """Only the network boundary is replaced; real storage and validation run."""
    def __init__(self, mode='completed', budget=10):
        self.mode, self.budget, self.calls = mode, budget, []

    def binding(self):
        return {'fixture': 'study-contract-v1', 'max_operations': self.budget}

    def prepare(self, payload):
        return copy.deepcopy(payload)

    def reply(self, payload):
        cites = [{'source_id': s['id'], 'anchor': s['excerpts'][0]['anchor'],
                  'quote': s['excerpts'][0]['quote'][:35]} for s in payload['sources']]
        if self.mode == 'bad_citation':
            cites[0]['quote'] = 'THIS TEXT WAS NEVER IN THE SOURCE'
        sections = []
        for role, kind in [('problem', 'author_claim'), ('mechanism', 'author_claim'),
                           ('example', 'example'), ('comparison', 'inference'), ('check', 'inference')]:
            sections.append({'role': role, 'kind': kind, 'title': role,
                             'text': '合成工程測試解說，不代表真實模型能力。',
                             'citations': cites if role == 'comparison' else cites[:1]})
        if payload['mode'] == 'qa':
            sections = [{'role': 'answer', 'kind': 'inference', 'title': '回答',
                         'text': '依所提供片段回答新的問題。', 'citations': cites[:1]}]
        return {'sections': sections, 'limitations': '只收到有限片段，不能推論所有實驗。'}

    def submit(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if self.mode == 'timeout':
            raise TimeoutError('SECRET MUST NOT LEAK')
        if self.mode == 'pending':
            return {'state': 'pending'}
        return {'state': 'completed', 'answer': self.reply(payload), 'usage_state': 'known'}

    def poll(self, operation_id):
        return {'state': 'completed', 'answer': self.reply(self.calls[-1]), 'usage_state': 'known'}


class StudyAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'hub.db'
        self.store = KnowledgeStore(self.path)
        self.focal, self.version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2601.00001', 'Episodic memory retrieval with consolidation',
            'Episodic memory retrieval stores observations and consolidates episodes. '
            'A query retrieves relevant episodes before generation.'))
        self.old, self.old_version, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2401.00001', 'Memory retrieval baseline',
            'Episodic memory retrieval uses similarity search over observations.'))
        self.noise, _, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2401.00002', 'Hardware allocation', 'This paper presents results for training. ' * 100))
        self.store.add_note(self.old, self.old_version, 'p1', 'PRIVATE_SENTINEL')
        import knowledge_hub
        self.assertTrue((Path(knowledge_hub.__file__).parent / 'study.py').exists(),
                        'Generic study workflow is missing')
        from knowledge_hub import study
        self.study = study
        self.provider = FakeProvider()
        self.service = study.StudyService(self.store, self.provider)

    def generate(self, request='lesson-1', **kw):
        return self.service.generate(self.focal, self.version, request,
                                     kw.pop('mode', 'lesson'), kw.pop('prompt', '理解機制與取捨'), **kw)

    def test_retrieval_prefers_specific_evidence_and_never_sends_private_notes(self):
        context = self.service.context(self.focal, self.version)
        self.assertEqual(context['sources'][1]['item_id'], self.old)
        self.assertIn('memory', context['sources'][1]['matched_terms'])
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(context['sources']))
        self.assertNotIn(self.noise, [s['item_id'] for s in context['sources']])
        row = self.generate()
        self.assertEqual(row['state'], 'completed')
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(self.provider.calls))
        self.assertTrue(row['answer']['sections'][0]['citations'][0]['reading_url'].endswith('#p1'))

    def test_completed_request_replays_but_conflict_does_not_dispatch(self):
        row = self.generate()
        self.assertEqual(self.generate()['id'], row['id'])
        with self.assertRaises(self.study.StudyError):
            self.generate(prompt='changed')
        self.assertEqual(len(self.provider.calls), 1)

    def test_two_column_bibliography_is_excluded_before_ranking(self):
        from knowledge_hub.study_context import readable_blocks
        version = {'blocks': [
            {'anchor': 'page-1', 'text': 'Useful discussion.       REFERENCES\n[1] Author. 2026. Memory retrieval graph.'},
            {'anchor': 'page-2', 'text': 'Retrieved 2026 from another memory graph source.'}]}
        selected = readable_blocks(version)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['text'].strip(), 'Useful discussion.')

    def test_teaching_evidence_covers_methods_and_experiments_beyond_overview(self):
        from knowledge_hub.study_context import study_spans
        blocks = [{'anchor': 'intro', 'text': '1 Introduction'}]
        blocks += [{'anchor': f'overview-{i}', 'text':
                    f'We propose a memory retrieval graph model with episodic memory consolidation and adaptive agents, variant number {i}.'}
                   for i in range(18)]
        blocks += [{'anchor': 'method', 'text': '2 Method'},
                   {'anchor': 'method-detail', 'text': 'First, merge matching nodes and then update their connections.'},
                   {'anchor': 'experiment', 'text': '3 Experiments'},
                   {'anchor': 'experiment-detail', 'text': 'The ablation removes the merge operation to compare its contribution.'}]
        selected = study_spans(blocks, {'memory','retrieval','graph','model','episodic','consolidation','agents'}, set())
        anchors = {blocks[i]['anchor'] for i, *_ in selected}
        self.assertIn('method-detail', anchors)
        self.assertIn('experiment-detail', anchors)
        self.assertLessEqual(len(selected), 12)

    def test_unknown_survives_restart_and_blocks_new_id(self):
        self.provider.mode = 'timeout'
        row = self.generate()
        self.assertEqual(row['state'], 'unknown')
        service = self.study.StudyService(KnowledgeStore(self.path), self.provider)
        with self.assertRaises(self.study.StudyError):
            service.generate(self.focal, self.version, 'new-id', 'qa', '新的問題')
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn('SECRET', json.dumps(service.records(self.focal)))

    def test_concurrent_requests_cannot_dispatch_while_first_is_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        original = self.provider.submit
        def delayed(payload):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test did not release provider')
            return original(payload)
        self.provider.submit = delayed
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.generate()))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            for request in ('lesson-1', 'concurrent-new-id'):
                with self.assertRaises(self.study.StudyError) as failure:
                    self.generate(request)
                self.assertEqual(failure.exception.code, 409)
        finally:
            release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]['state'], 'completed')
        self.assertEqual(len(self.provider.calls), 1)

    def test_legacy_unknown_blocks_new_feature_before_provider(self):
        from knowledge_hub import qa
        with self.assertRaises(qa.QAError):
            qa.ask(self.store, lambda _: (_ for _ in ()).throw(TimeoutError()),
                   self.focal, self.version, 'legacy', '舊問題')
        with self.assertRaises(self.study.StudyError):
            self.generate()
        self.assertFalse(self.provider.calls)

    def test_invalid_citation_is_not_saved_as_teaching(self):
        self.provider.mode = 'bad_citation'
        row = self.generate()
        self.assertEqual(row['state'], 'failed')
        self.assertIsNone(row['answer'])
        self.assertNotIn('THIS TEXT', json.dumps(self.store.export_data()))

    def test_new_question_uses_parent_context_and_original_sources(self):
        lesson = self.generate()
        answer = self.generate('question-2', mode='qa', prompt='如果沒有相近 episode 會怎樣？', parent_id=str(lesson['id']))
        self.assertEqual(answer['state'], 'completed')
        payload = self.provider.calls[-1]
        self.assertEqual(payload['parent_id'], lesson['id'])
        self.assertTrue(payload['conversation'])
        self.assertEqual(payload['prompt'], '如果沒有相近 episode 會怎樣？')
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(payload))

    def test_save_restart_search_and_duplicate_save(self):
        row = self.generate()
        saved = self.service.save_progress(self.focal, self.version, 'save-1', str(row['id']),
                                           '我理解 episodic 輸入', '仍不清楚 consolidation 的條件')
        again = self.service.save_progress(self.focal, self.version, 'save-1', str(row['id']),
                                           '我理解 episodic 輸入', '仍不清楚 consolidation 的條件')
        self.assertEqual(saved['id'], again['id'])
        store = KnowledgeStore(self.path)
        resumed = self.study.StudyService(store).progress(self.focal, self.version)
        self.assertEqual(resumed[0]['understanding'], '我理解 episodic 輸入')
        self.assertTrue(store.search('仍不清楚'))
        self.assertEqual(len(store.get_lessons()), 1)
        later = self.study.StudyService(store).context(self.old, self.old_version)
        self.assertIn(saved['id'], [x['id'] for x in later['saved_learning']])
        self.assertNotIn('仍不清楚', json.dumps(later['sources']))

    def test_operation_budget_persists_across_restart(self):
        self.provider.budget = 1
        self.generate()
        service = self.study.StudyService(KnowledgeStore(self.path), self.provider)
        with self.assertRaises(self.study.StudyError):
            service.generate(self.focal, self.version, 'budget-2', 'qa', '再問一次')
        self.assertEqual(len(self.provider.calls), 1)

    def test_evidence_id_learning_loop_saves_and_resumes_without_private_context(self):
        from knowledge_hub.broker import StudyBrokerProvider
        original = self.provider.reply
        wires = []
        wire_provider = object.__new__(StudyBrokerProvider)
        def evidence_reply(payload):
            wire = json.loads(wire_provider.prompt(payload)[len(payload['instructions']) + 1:])
            wires.append(wire)
            identifiers = {s['id']: s['excerpts'][0]['evidence_id'] for s in wire['sources']}
            answer = original(payload)
            for section in answer['sections']:
                section['citations'] = [{'evidence_id': identifiers[c['source_id']]}
                                        for c in section['citations']]
            return answer
        self.provider.reply = evidence_reply
        lesson = self.generate()
        self.assertEqual(lesson['state'], 'completed')
        answer = self.generate('id-question', mode='qa', prompt='沒有相近記憶時怎麼辦？',
                               parent_id=str(lesson['id']))
        self.assertEqual(answer['state'], 'completed')
        saved = self.service.save_progress(self.focal, self.version, 'id-save', str(answer['id']),
                                           'PRIVATE_UNDERSTANDING', 'PRIVATE_OPEN_QUESTION')
        restarted = self.study.StudyService(KnowledgeStore(self.path), self.provider)
        self.assertEqual(restarted.progress(self.focal, self.version)[0]['id'], saved['id'])
        self.assertEqual(restarted.progress(self.focal, self.version)[0]['understanding'],
                         'PRIVATE_UNDERSTANDING')
        resumed = restarted.generate(self.focal, self.version, 'id-resume', 'qa',
                                     '請從上一個回答繼續解釋限制。', parent_id=str(answer['id']))
        self.assertEqual(resumed['state'], 'completed')
        self.assertEqual(resumed['parent_id'], answer['id'])
        self.assertEqual(len(wires[-1]['conversation']), 2)
        self.assertEqual(len(self.provider.calls), 3)
        for secret in ('PRIVATE_SENTINEL', 'PRIVATE_UNDERSTANDING', 'PRIVATE_OPEN_QUESTION'):
            self.assertNotIn(secret, json.dumps(wires))
            self.assertNotIn(secret, json.dumps(self.provider.calls))
        for row in restarted.records(self.focal):
            for section in row['answer']['sections']:
                for cite in section['citations']:
                    self.assertNotIn('evidence_id', cite)
                    self.assertTrue(cite['reading_url'])
                    source = next(s for s in row['payload']['sources'] if s['id'] == cite['source_id'])
                    self.assertTrue(any(cite['quote'] in e['quote'] for e in source['excerpts']
                                        if e['anchor'] == cite['anchor']))

    def test_long_valid_teaching_does_not_block_personal_progress(self):
        original = self.provider.reply
        def long_reply(payload):
            answer = original(payload)
            for section in answer['sections']:
                section['text'] = 'Evidence remains unchanged. ' * 145
            return answer
        self.provider.reply = long_reply
        row = self.generate()
        self.assertEqual(row['state'], 'completed')
        self.assertGreater(len(self.study.markdown(row)), 16000)
        saved = self.service.save_progress(self.focal, self.version, 'long-save', str(row['id']),
                                           '完整保存理解', '繼續核對問題')
        self.assertEqual(saved['record_id'], row['id'])
        self.assertIsNone(saved['lesson_id'])
        self.assertIn('16000', saved['export_error'])
        resumed = self.study.StudyService(KnowledgeStore(self.path))
        self.assertEqual(resumed.progress(self.focal, self.version)[0], saved)
        self.assertEqual(resumed.records(self.focal)[0]['answer'], row['answer'])
        from knowledge_hub.web import create_app
        _, _, html = self.request(create_app(self.path), f'/items/{self.focal}')
        self.assertIn('未建立額外來源包', html)
        self.assertIn('完整保存理解', html)

    def test_pending_binding_change_cannot_reconcile_or_resubmit(self):
        self.provider.mode = 'pending'
        row = self.generate()
        self.provider.budget = 3
        with self.assertRaises(self.study.StudyError):
            self.service.reconcile(row['id'])
        self.assertEqual(self.service.records(self.focal)[0]['state'], 'pending')
        self.provider.budget = 10
        self.assertEqual(self.service.reconcile(row['id'])['state'], 'completed')

    def test_legacy_routes_cannot_bypass_study_unknown(self):
        from knowledge_hub import qa, connections
        self.provider.mode = 'timeout'
        self.generate()
        called = []
        with self.assertRaises(qa.QAError) as failure:
            qa.ask(self.store, lambda p: called.append(p), self.focal,
                   self.version, 'legacy-new', '再次詢問')
        self.assertEqual(failure.exception.code,409)
        self.assertFalse(called)
        with self.assertRaises(connections.ConnectionError) as failure:
            connections.ConnectionService(self.store).generate(
                self.focal, self.version, 'connection-new')
        self.assertEqual(failure.exception.code,409)

    def test_export_carries_progress_and_evidence(self):
        row = self.generate()
        self.service.save_progress(self.focal,self.version,'export-1',str(row['id']),'保存理解','未解問題')
        exported = self.store.export_data()
        self.assertEqual(exported['study']['progress'][0]['questions'], '未解問題')
        self.assertEqual(exported['study']['records'][0]['payload']['sources'][0]['version_id'], self.version)

    def test_broker_prepare_preserves_exact_evidence_and_explicit_bounds(self):
        from knowledge_hub import broker
        self.assertTrue(hasattr(broker, 'StudyBrokerProvider'), 'Bounded study provider is missing')
        token = Path(self.tmp.name) / 'token'; token.write_text('synthetic-secret'); token.chmod(0o600)
        provider = broker.StudyBrokerProvider('http://127.0.0.1:19999',str(token),'fixture','fixture',
            max_output_tokens=4096,max_input_bytes=16000,max_operations=3,session_id='test-session')
        self.generate()
        payload = copy.deepcopy(self.provider.calls[-1])
        payload['provider_binding'] = provider.binding()
        prepared = provider.prepare(payload)
        task = provider.task(prepared)
        self.assertEqual(task['max_output_tokens'],4096)
        self.assertEqual(task['max_attempts'],1)
        self.assertNotIn('synthetic-secret',json.dumps(task))
        self.assertLessEqual(len(broker.encoded({'input':task['input'],'options':{}}).encode()),16000)
        self.assertEqual(prepared['sources'],payload['sources'])

    def test_bounded_history_preserves_records_privacy_and_three_operation_budget(self):
        from knowledge_hub import broker
        from knowledge_hub.reading import ReadingState
        token=Path(self.tmp.name)/'synthetic-token'
        token.write_text('synthetic-secret');token.chmod(0o600)
        bounded=broker.StudyBrokerProvider('http://127.0.0.1:19999',str(token),'fixture','fixture',
            max_output_tokens=8192,max_input_bytes=32768,max_operations=3,session_id='history-budget')
        # Use the real preparation contract; only submission is the existing
        # synthetic answer boundary. No socket or external SDK is invoked.
        self.provider.binding=bounded.binding
        self.provider.prepare=bounded.prepare
        reply=self.provider.reply
        def long_reply(payload):
            result=reply(payload)
            for section in result['sections']:
                section['text']='公開機制。'*(240 if payload['mode']=='lesson' else 1200)
            return result
        self.provider.reply=long_reply
        ReadingState(self.store).save(self.focal,self.version,0,
            {'reflection':'PRIVATE_REFLECTION','question_draft':'PRIVATE_UNSENT'})
        lesson=self.generate('bounded-lesson')
        first=self.generate('bounded-first',mode='qa',prompt='第一次追問',parent_id=lesson['id'])
        original_answers=[copy.deepcopy(lesson['answer']),copy.deepcopy(first['answer'])]
        second=self.generate('bounded-second',mode='qa',prompt='目前問題不得裁切',parent_id=first['id'])
        self.assertEqual(second['state'],'completed')
        self.assertIn('省略',second['payload']['limitations'])
        self.assertEqual(second['payload']['conversation'][-1]['prompt'],'第一次追問')
        self.assertEqual(second['payload']['prompt'],'目前問題不得裁切')
        self.assertEqual([r['answer'] for r in self.service.records(self.focal)[:2]],original_answers)
        self.assertNotIn('PRIVATE_',json.dumps(second['payload'],ensure_ascii=False))
        self.assertEqual(second['payload'],self.provider.calls[-1])
        task=bounded.task(second['payload'])
        self.assertLessEqual(len(broker.encoded({'input':task['input'],'options':{}}).encode()),32768)
        # Every final citation must refer to exactly the source snapshot stored
        # and sent for this operation, not to an omitted historical answer.
        self.assertEqual(self.study.validate_answer(
            {**second['answer'], 'sections':[{**section,'citations':[
                {k:c[k] for k in ('source_id','anchor','quote')} for c in section['citations']]}
                for section in second['answer']['sections']]},second['payload']['sources'],'qa'),second['answer'])
        with self.assertRaises(self.study.StudyError):
            self.generate('bounded-fourth',mode='qa',prompt='不能繞過三次上限',parent_id=second['id'])
        self.assertEqual(len(self.provider.calls),3)

    def test_history_preparation_rejects_fabrication_and_missing_disclosure(self):
        lesson=self.generate('history-parent')
        from knowledge_hub.study import history_variants
        mutations=('invented_answer','invented_citation','changed_question','missing_disclosure')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                def tamper(payload):
                    options=list(history_variants(payload['conversation'],payload['limitations']))
                    payload['conversation'],payload['limitations']=copy.deepcopy(options[1])
                    if mutation=='invented_answer':
                        payload['conversation'][0]['answer']['sections'][0]['text']='INVENTED'
                    elif mutation=='invented_citation':
                        payload['conversation'][0]['answer']['sections'][0]['citations']=[
                            {'source_id':'S1','anchor':'p1','quote':'INVENTED'}]
                    elif mutation=='changed_question':
                        payload['prompt']='DIFFERENT CURRENT QUESTION'
                    else:
                        payload['limitations']=self.service.context(self.focal,self.version,'接著問')['limitations']
                    return payload
                self.provider.prepare=tamper
                with self.assertRaises(self.study.StudyError) as failure:
                    self.generate('history-'+mutation,mode='qa',prompt='接著問',parent_id=lesson['id'])
                self.assertEqual(failure.exception.code,400)
        self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(len(self.service.records(self.focal)),1)

    def test_broker_refuses_missing_completion_metadata(self):
        from knowledge_hub import broker
        self.assertTrue(hasattr(broker, 'StudyBrokerProvider'), 'Bounded study provider is missing')
        token = Path(self.tmp.name) / 'token';token.write_text('synthetic-secret');token.chmod(0o600)
        provider = broker.StudyBrokerProvider('http://127.0.0.1:19999',str(token),'fixture','fixture',
            max_output_tokens=4096,max_input_bytes=16000,max_operations=3,session_id='test-session')
        outcome={'state':'completed','result':{'text':'{"sections":[],"limitations":"缺證據"}'}}
        with patch.object(provider,'_http',side_effect=[(200,{'state':'completed'}),(200,outcome)]):
            self.assertEqual(provider.poll('original-operation')['state'],'failed')

    def request(self, app, path, fields=None, origin='http://127.0.0.1:8765'):
        env = {};setup_testing_defaults(env)
        parsed=urlsplit(path)
        env.update(PATH_INFO=parsed.path,QUERY_STRING=parsed.query,HTTP_HOST='127.0.0.1:8765',SERVER_PORT='8765')
        if fields is not None:
            data=urlencode({'csrf':self.store.get_csrf_token(),**fields}).encode()
            env.update(REQUEST_METHOD='POST',CONTENT_TYPE='application/x-www-form-urlencoded',
                       CONTENT_LENGTH=str(len(data)),HTTP_ORIGIN=origin)
            env['wsgi.input']=io.BytesIO(data)
        status=[]
        body=b''.join(app(env,lambda code,headers:status.extend([code,dict(headers)]))).decode()
        return status[0],status[1],body

    def test_web_generation_followup_save_and_resume_stay_on_reading_page(self):
        from knowledge_hub.web import create_app
        app=create_app(self.path,study_provider=self.provider)
        path=f'/items/{self.focal}'
        status,_,html=self.request(app,path)
        self.assertEqual(status,'200 OK');self.assertIn('開始教我',html)
        self.assertNotIn('id="study-prompt"',html)
        self.assertFalse(self.provider.calls)
        fields={'version_id':self.version,'request_id':'web-lesson','mode':'lesson','prompt':'學習機制'}
        status,headers,_=self.request(app,path+'/study',fields)
        self.assertEqual(status,'303 See Other');self.assertIn('#study-',headers['Location'])
        row=self.service.records(self.focal)[-1]
        status,_,html=self.request(app,path+'/progress',{'version_id':self.version,'request_id':'web-save',
            'record_id':str(row['id']),'understanding':'瀏覽器保存理解','questions':'如何避免資料遺失？'})
        self.assertEqual(status,'303 See Other')
        app=create_app(self.path)
        _,_,html=self.request(app,path)
        self.assertIn('瀏覽器保存理解',html);self.assertIn('如何避免資料遺失？',html)
        _,_,home=self.request(app,'/')
        self.assertIn('接續閱讀',home)

    def test_web_error_preserves_input_and_enforces_origin(self):
        from knowledge_hub.web import create_app
        app=create_app(self.path)
        path=f'/items/{self.focal}/study'
        fields={'version_id':self.version,'request_id':'web-disabled','mode':'qa','prompt':'尚未送出的問題 <script>hi</script>'}
        status,_,body=self.request(app,path,fields)
        self.assertEqual(status,'503 Service Unavailable')
        self.assertIn('尚未送出的問題 &lt;script&gt;hi&lt;/script&gt;',body)
        self.assertFalse(self.service.records(self.focal))
        status,_,_=self.request(app,path,fields,origin='https://evil.example')
        self.assertEqual(status,'403 Forbidden')

    def test_progress_error_preserves_selected_teaching_including_none(self):
        from html.parser import HTMLParser
        from knowledge_hub.web import create_app
        class Selection(HTMLParser):
            def __init__(self):
                super().__init__(); self.selected = []
            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == 'input' and values.get('name') == 'record_id':
                    self.selected.append(values.get('value'))
        lesson = self.generate()
        self.generate('followup', mode='qa', prompt='如何接續？', parent_id=str(lesson['id']))
        app = create_app(self.path)
        for selection in ('', str(lesson['id'])):
            with self.subTest(selection=selection):
                status, _, html = self.request(app, f'/items/{self.focal}/progress', {
                    'version_id': self.version, 'request_id': 'invalid-save', 'record_id': selection,
                    'understanding': '字' * 8001, 'questions': '保留這個問題'})
                self.assertEqual(status, '400 Bad Request')
                parser = Selection(); parser.feed(html)
                self.assertEqual(parser.selected, [selection])
                self.assertIn('保留這個問題', html)

    def test_cli_requires_explicit_study_limits_and_wires_only_study_provider(self):
        from knowledge_hub.__main__ import main, build_parser
        options={a.dest for a in build_parser()._actions}
        self.assertIn('enable_study',options)
        token=Path(self.tmp.name)/'token';token.write_text('synthetic-secret');token.chmod(0o600)
        args=['--db',str(self.path),'--enable-study','--broker-base-url','http://127.0.0.1:19999',
              '--broker-token-file',str(token),'--broker-provider','fixture','--broker-model','fixture']
        with patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):
            main(args)
        with patch('knowledge_hub.__main__.create_app') as app,patch('knowledge_hub.__main__.make_server'):
            self.assertEqual(main(args+['--study-max-input-bytes','32768','--study-max-output-tokens','4096',
                '--study-max-operations','4','--study-session-id','acceptance-v1']),0)
            self.assertIsNone(app.call_args.kwargs['connection_provider'])
            self.assertEqual(app.call_args.kwargs['study_provider'].binding()['max_operations'],4)


if __name__ == '__main__':
    unittest.main()
