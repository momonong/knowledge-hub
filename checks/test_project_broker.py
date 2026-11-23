"""SDK 2.0 boundary: fake network only, with actual KH payload validation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ClientFailure(Exception):
    def __init__(self, error):
        self.detail = {'error': error, 'message': 'DO NOT DISPLAY SECRET'}


class Client:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.reply = {'state': 'queued'}
        self.outcome = {'state': 'completed_usage_unknown', 'finish_reason': 'stop',
                        'response_truncated': False, 'result': {'text': '{"sections":[]}'}}

    def submit(self, task):
        self.calls.append(copy.deepcopy(task))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def status(self, key):
        self.calls.append(('status', key))
        return self.reply

    def result(self, key):
        self.calls.append(('result', key))
        return self.outcome


class ProjectAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        folder.chmod(0o700)
        token = folder / 'token'
        token.write_text('synthetic-project-token-not-real-123456')
        token.chmod(0o600)
        profile = folder / 'profile.json'
        profile.write_text('{"fixture":"public-root"}')
        from knowledge_hub.project_broker import ProjectStudyProvider
        self.provider = ProjectStudyProvider('https://momonong.me/quota/api/v1', token,
            'google', 'gemini-2.5-flash', trust_profile=profile,
            trust_state=folder / 'trust.sqlite', max_input_bytes=32768,
            max_output_tokens=8192, max_operations=3, session_id='test-only',
            client_factory=Client)
        self.client = self.provider._client
        self.payload = {'operation_id': 'persisted-operation-1', 'instructions': 'Public instruction',
            'mode':'lesson', 'prompt':'Public question', 'sources':[], 'conversation':[],
            'limitations':'Partial public excerpts', 'provider_binding':self.provider.binding()}

    def test_submit_is_once_encrypted_cloud_explicit_and_bounded(self):
        result = self.provider.submit(self.payload)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['trace']['broker_status']['state'], 'queued')
        task = self.client.calls[0]
        self.assertEqual(task['request_key'], 'persisted-operation-1')
        self.assertEqual(task['execution_policy'], 'cloud_only')
        self.assertEqual(task['max_attempts'], 1)
        self.assertEqual(task['max_output_tokens'], 8192)
        self.assertNotIn('token', json.dumps(self.provider.binding()).replace('max_output_tokens',''))

    def test_unknown_transport_and_404_never_retry_submit(self):
        for code in ['transport_unknown', 'not_found', 'conflict']:
            self.client.reply = ClientFailure(code)
            result = self.provider.submit(self.payload)
            self.assertEqual(result['state'], 'unknown')
            self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len(self.client.calls), 3)

    def test_poll_recovers_original_key_and_separates_usage(self):
        self.client.reply = {'state':'completed', 'result_available':True}
        result = self.provider.poll('persisted-operation-1')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['usage_state'], 'unknown')
        self.assertEqual(self.client.calls, [('status','persisted-operation-1'),('result','persisted-operation-1')])

    def test_unknown_stops_without_fetching_result_and_incomplete_output_rejected(self):
        self.client.reply = {'state':'unknown', 'result_available':True}
        self.assertEqual(self.provider.poll('original')['state'], 'unknown')
        self.assertEqual(len(self.client.calls), 1)
        self.client.reply = {'state':'completed', 'result_available':True}
        for finish in [None, 'length', 'content_filter']:
            self.client.outcome['finish_reason'] = finish
            self.assertEqual(self.provider.poll('original')['state'], 'failed')

    def test_pending_storage_is_not_a_new_attempt(self):
        self.client.reply = {'state':'completed','result_available':False,'result_state':'storage_failed'}
        self.assertEqual(self.provider.poll('original')['state'], 'pending')
        self.assertEqual(self.client.calls, [('status','original')])

    def test_explicit_original_readback_shows_broker_deadline_and_keeps_unknown(self):
        self.client.reply = {'state':'unknown','result_available':True,'error_code':'execution_uncertain'}
        self.client.outcome = {'state':'unknown','error_code':'family_total_deadline','latency_ms':60052,
                               'diagnostics':{'message':'private upstream diagnostic'}}
        result = self.provider.inspect_unknown('original')
        self.assertEqual(self.client.calls,[('status','original'),('result','original')])
        self.assertEqual((result['state'],result['reason_code']),('unknown','provider_deadline'))
        self.assertEqual(result['trace']['broker_result']['error_code'],'family_total_deadline')
        self.assertNotIn('private upstream diagnostic',json.dumps(result))

    def test_expired_result_shows_retained_deadline_from_original_status(self):
        self.client.reply = {'state': 'unknown', 'error_code': 'execution_uncertain',
                             'gateway_error_code': 'family_total_deadline', 'result_available': False,
                             'ledger_basis': 'held_estimate', 'message': 'private upstream detail'}
        reply = self.provider.inspect_unknown('original')
        self.assertEqual((reply['state'], reply['reason_code']), ('unknown', 'provider_deadline'))
        self.assertEqual(self.client.calls, [('status', 'original')])
        self.assertNotIn('private upstream detail', json.dumps(reply))

    def test_unrecognized_retained_codes_keep_unknown_generic(self):
        for code in (None, True, ['family_total_deadline'], {'message': 'private upstream detail'},
                     'FAMILY_TOTAL_DEADLINE', 'family_total_deadline ', 'family_total_deadline: private upstream detail',
                     'provider_http_error'):
            with self.subTest(code=code):
                self.client.calls.clear()
                self.client.reply = {'state': 'unknown', 'error_code': 'execution_uncertain',
                                     'result_available': False, 'gateway_error_code': code}
                reply = self.provider.inspect_unknown('original')
                self.assertEqual((reply['state'], reply['reason_code']), ('unknown', 'provider_unknown'))
                self.assertEqual(self.client.calls, [('status', 'original')])
                self.assertNotIn('private upstream detail', json.dumps(reply))

    def test_provider_http_error_is_explained_without_clearing_unknown(self):
        from knowledge_hub.connections import REASON_LABELS
        from knowledge_hub.store import KnowledgeStore
        from knowledge_hub.study import StudyService, StudyError
        from test_study import entry
        store = KnowledgeStore(Path(self.tmp.name) / 'diagnostic.sqlite3')
        item, version, _ = store.upsert_parsed_entry(entry('arxiv:2601.00001',
            'Public memory paper', 'A public memory system retrieves evidence.'))
        service = StudyService(store, self.provider)
        row = service.generate(item, version, 'diagnostic-one', 'lesson', 'Explain memory.')
        self.client.reply = {'state':'unknown', 'result_available':True, 'http_status':503,
                             'diagnostics':{'message':'SECRET must never reach UI'}}
        saved = service.reconcile(row['id'])
        self.assertEqual(saved['state'], 'unknown')
        self.assertEqual(saved['reason_code'], 'provider_unavailable')
        self.assertIn('503', REASON_LABELS[saved['reason_code']])
        self.assertNotIn('SECRET', json.dumps(saved))
        restarted = StudyService(KnowledgeStore(Path(self.tmp.name) / 'diagnostic.sqlite3'), self.provider)
        self.assertFalse(restarted.availability()['available'])
        self.assertEqual(restarted.availability()['used'], 1)
        with self.assertRaises(StudyError):
            restarted.generate(item, version, 'diagnostic-two', 'lesson', 'A different question.')
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.client.calls[-1], ('status', row['operation_id']))

    def test_unknown_http_reason_uses_only_validated_status_codes(self):
        for status, reason in [(401,'provider_auth_rejected'), (403,'provider_auth_rejected'),
                               (503,'provider_unavailable'), (500,'provider_server_error'),
                               ('503 SECRET','provider_unknown'), (True,'provider_unknown'),
                               (200,'provider_unknown')]:
            with self.subTest(status=status):
                self.client.reply = {'state':'unknown', 'http_status':status}
                result = self.provider.poll('original')
                self.assertEqual(result['state'], 'unknown')
                self.assertEqual(result['reason_code'], reason)
                self.assertNotIn('SECRET', json.dumps(result))

    def test_binding_or_input_limit_change_prevents_submit(self):
        self.payload['prompt'] = 'x'*40000
        with self.assertRaises(ValueError):
            self.provider.submit(self.payload)
        self.payload['prompt'] = 'Public question'
        self.payload['provider_binding']['model'] = 'different'
        with self.assertRaises(ValueError):
            self.provider.submit(self.payload)
        self.assertFalse(self.client.calls)

    def test_long_chinese_lesson_and_two_followups_prepare_without_submit(self):
        from knowledge_hub.study import INSTRUCTIONS, validate_answer
        source = {'id':'S1', 'item_id':1, 'version_id':'1', 'title':'Public evidence', 'scope':'full',
                  'excerpts':[{'anchor':f'p{i}', 'quote':'Evidence for mechanism. ' + 'x'*1976,
                    'source_url':'https://example.org/paper', 'reading_url':f'/items/1?version=1#p{i}'}
                    for i in range(6)]}
        citation = {'source_id':'S1', 'anchor':'p0', 'quote':'Evidence for mechanism.'}
        lesson = {'sections':[{'role':role, 'kind':kind, 'title':role, 'text':'機制解釋。'*120,
                             'citations':[citation]} for role,kind in
                  [('problem','author_claim'), ('mechanism','author_claim'), ('example','example'),
                   ('comparison','inference'), ('check','inference')]], 'limitations':'合成容量檢查。'}
        answer = {'sections':[{'role':'answer','kind':'inference','title':'回答','text':'機制解釋。'*600,
                               'citations':[citation]}], 'limitations':'合成容量檢查。'}
        history = [{'prompt':'原教學問題', 'answer':validate_answer(lesson,[source],'lesson')},
                   {'prompt':'最近一次追問', 'answer':validate_answer(answer,[source],'qa')}]
        for count in range(3):
            with self.subTest(prior_turns=count):
                payload = {**self.payload, 'instructions':INSTRUCTIONS, 'mode':'qa' if count else 'lesson',
                           'sources':[source], 'prompt':'目前問題不可裁切。', 'conversation':history[:count]}
                original = copy.deepcopy(payload)
                task = self.provider.task(payload)
                before = len(json.dumps({'input':task['input'],'options':{}},ensure_ascii=False,
                                         separators=(',',':')).encode('utf-8'))
                if count == 2:
                    self.assertGreater(before,32768)
                try:
                    prepared = self.provider.prepare(payload)
                except ValueError as exc:
                    self.fail(f'合法的長教學接續不應因公開歷史超限而中斷：{exc}')
                task = self.provider.task(prepared)
                self.assertLessEqual(len(json.dumps({'input':task['input'],'options':{}},ensure_ascii=False,
                                                   separators=(',',':')).encode('utf-8')),32768)
                self.assertEqual(payload,original)
                self.assertEqual(prepared['prompt'],original['prompt'])
                self.assertEqual(prepared['sources'],original['sources'])
                if count == 2:
                    self.assertEqual(prepared['conversation'][-1],history[-1])
                    self.assertIn('省略',prepared['limitations'])
                    self.assertNotEqual(prepared['conversation'],history)
                self.assertEqual(prepared,self.provider.prepare(payload))
        # A long most-recent answer must not permanently block follow-ups,
        # even after older turns have already been omitted.
        latest=copy.deepcopy(history[-1])
        latest['answer']['sections'][0]['text']='最近回答。'*1200
        payload={**payload, 'conversation':[latest]}
        prepared=self.provider.prepare(payload)
        selected=prepared['conversation'][-1]
        self.assertEqual(selected['prompt'],latest['prompt'])
        excerpt=selected['answer']['sections'][0]['text']
        self.assertTrue(latest['answer']['sections'][0]['text'].startswith(excerpt))
        self.assertLess(len(excerpt),len(latest['answer']['sections'][0]['text']))
        self.assertEqual(selected['answer']['sections'][0]['citations'],[])
        self.assertEqual(prepared['sources'],[source])
        self.assertIn('省略',prepared['limitations'])
        # If the old questions themselves fill the envelope, omit oldest whole
        # turns only after reducing answer detail; keep the latest question.
        payload['conversation']=[{'prompt':str(i)+'舊問題。'*499,'answer':answer} for i in range(4)]
        prepared=self.provider.prepare(payload)
        self.assertLess(len(prepared['conversation']),4)
        self.assertGreaterEqual(len(prepared['conversation']),1)
        self.assertEqual(prepared['conversation'][-1]['prompt'],payload['conversation'][-1]['prompt'])
        self.assertTrue(self.provider._fits(prepared))
        self.assertFalse(self.client.calls,'prepare must never submit, poll, or fetch a result')

    def test_prepare_cannot_trim_current_question_to_fit_evidence(self):
        self.payload.update(prompt='當前問題。'*400,
                            sources=[{'id':'S1','title':'Public','scope':'full',
                                      'excerpts':[{'anchor':f'p{i}','quote':'原文'*1000} for i in range(6)]}])
        original=copy.deepcopy(self.payload)
        with self.assertRaises(ValueError):
            self.provider.prepare(self.payload)
        self.assertEqual(self.payload,original)
        self.assertFalse(self.client.calls)

    def test_prepare_enforces_exact_normalized_utf8_byte_boundary(self):
        task=self.provider.task(self.payload)
        size=len(json.dumps({'input':task['input'],'options':{}},ensure_ascii=False,
                            separators=(',',':')).encode('utf-8'))
        self.payload['instructions'] += 'x'*(32768-size)
        self.assertEqual(self.provider.prepare(self.payload),self.payload)
        self.payload['instructions'] += '字'
        with self.assertRaises(ValueError):
            self.provider.prepare(self.payload)
        self.assertFalse(self.client.calls)

    def test_cli_wires_encrypted_provider_with_persistent_trust_paths(self):
        from knowledge_hub.__main__ import main
        folder=Path(self.tmp.name)
        args=['--db',str(folder/'test.sqlite3'),'--enable-study','--broker-protocol','encrypted',
              '--broker-base-url','https://momonong.me/quota/api/v1',
              '--broker-token-file',str(folder/'token'),'--broker-provider','google',
              '--broker-model','gemini-3.5-flash-lite','--broker-trust-profile',str(folder/'profile.json'),
              '--broker-trust-state',str(folder/'trust.sqlite'),'--study-max-input-bytes','32768',
              '--study-max-output-tokens','8192','--study-max-operations','3','--study-session-id','test-only']
        with patch('knowledge_hub.project_broker.ProjectStudyProvider',return_value=self.provider) as factory, \
             patch('knowledge_hub.__main__.create_app') as app,patch('knowledge_hub.__main__.make_server'):
            self.assertEqual(main(args),0)
            self.assertEqual(factory.call_args.kwargs['trust_state'],str(folder/'trust.sqlite'))
            self.assertIs(app.call_args.kwargs['study_provider'],self.provider)
            self.assertIsNone(app.call_args.kwargs['connection_provider'])

    def test_answer_contract_separates_instructions_and_current_turn_data(self):
        from knowledge_hub.study import INSTRUCTIONS
        self.payload.update(instructions=INSTRUCTIONS, mode='qa',
            conversation=[{'prompt':'舊問題', 'answer': {
                'sections':[{'role':'answer', 'kind':'inference', 'title':'舊標題',
                             'text':'舊判斷', 'citations':[{'quote':'OLD_CITATION_SENTINEL'}]}],
                'limitations':'前次有限片段。'}}])
        messages=self.provider.task(self.payload)['input']['messages']
        self.assertEqual([message['role'] for message in messages], ['system','user'])
        contract, turn = messages[0]['content'], json.loads(messages[1]['content'])
        self.assertEqual(contract, INSTRUCTIONS)
        self.assertIn('第一筆從 S1 選', contract)
        self.assertIn('第二筆從背景來源選', contract)
        self.assertIn('不得複製輸入', contract)
        self.assertIn('不能推成改了模型權重', contract)
        self.assertEqual(turn['prompt'], self.payload['prompt'])
        self.assertEqual(turn['conversation'][0]['answer']['sections'][0]['text'], '舊判斷')
        self.assertNotIn('OLD_CITATION_SENTINEL', json.dumps(turn))
        self.assertIn('OLD_CITATION_SENTINEL', json.dumps(self.payload))
        self.assertEqual(self.client.calls, [])

    def test_duplicate_output_fields_are_not_silently_reconciled(self):
        self.client.reply = {'state':'completed','result_available':True}
        self.client.outcome['result']['text'] = '{"sections":["first"],"sections":[],"limitations":"end"}'
        reply = self.provider.poll('persisted-operation-duplicate')
        self.assertEqual(reply['state'], 'failed')
        self.assertEqual(reply['reason_code'], 'invalid_json')
        self.assertNotIn('answer', reply)
        self.assertEqual(self.client.calls, [('status','persisted-operation-duplicate'),
                                             ('result','persisted-operation-duplicate')])

    def test_sdk_fixture_completes_valid_parent_followup_and_rejects_recorded_bad_shapes(self):
        from knowledge_hub.store import KnowledgeStore
        from knowledge_hub.study import StudyService, StudyError
        from test_study import entry

        self.provider.max_operations = 5  # Synthetic SDK receipts only.
        db = Path(self.tmp.name) / 'contract.db'
        store = KnowledgeStore(db)
        item, version, _ = store.upsert_parsed_entry(entry('arxiv:2601.00001',
            'Episodic memory retrieval with consolidation',
            'Episodic memory retrieval consolidates episodes before looking up a query. '
            + ' '.join(f'Trace {i} stores the observed episode before a query retrieves the candidate.'
                       for i in range(17))))
        background, _, _ = store.upsert_parsed_entry(entry('arxiv:2401.00001',
            'Episodic memory retrieval baseline',
            'Episodic memory retrieval uses similarity search over stored observations.'))
        service = StudyService(store, self.provider)
        roles = [('problem','author_claim'),('mechanism','author_claim'),('example','example'),
                 ('comparison','inference'),('check','inference')]

        def submit(request, mode, parent=None):
            self.client.reply = {'state':'queued'}
            row = service.generate(item, version, request, mode, '如何檢索記憶？', parent)
            self.assertEqual(row['state'], 'pending')
            sent = self.client.calls[-1]
            self.assertEqual(sent['request_key'], row['operation_id'])
            self.assertEqual(sent['max_attempts'], 1)
            return row, json.loads(sent['input']['messages'][-1]['content'])

        def answer(wire, mode):
            sources = wire['sources']
            self.assertEqual([source['id'] for source in sources], ['S1','S2'])
            citations = [{'evidence_id': source['excerpts'][0]['evidence_id']} for source in sources]
            return {'sections':[{'role':role,'kind':kind,'title':role,
                    'text':'合成路徑涵蓋已選公開片段。以下是假設例子。',
                    'citations': citations if role == 'comparison' else citations[:1]}
                    for role,kind in (roles if mode == 'lesson' else [('answer','inference')])],
                    'limitations':'僅以合成內容驗證離線流程與已知證據歸屬。'}

        def terminal(row, result):
            self.client.reply = {'state':'completed','result_available':True,'http_status':200}
            self.client.outcome.update(state='completed', http_status=200, finish_reason='stop')
            self.client.outcome['result']['text'] = json.dumps(result)
            saved = service.reconcile(row['id'])
            self.assertEqual(self.client.calls[-2:], [('status',row['operation_id']),
                                                     ('result',row['operation_id'])])
            return saved

        row, wire = submit('synthetic-lesson','lesson')
        valid_lesson = terminal(row, answer(wire, 'lesson'))
        self.assertEqual(valid_lesson['state'], 'completed')
        self.assertEqual([ref['item_id'] for ref in valid_lesson['answer']['sections'][3]['citations']],
                         [item, background])
        service.save_progress(item, version, 'fake-save', valid_lesson['id'],
                              'SYNTHETIC_PRIVATE_UNDERSTANDING', 'SYNTHETIC_PRIVATE_QUESTION')
        service = StudyService(KnowledgeStore(db), self.provider)
        row, wire = submit('synthetic-followup','qa',valid_lesson['id'])
        self.assertEqual(len(wire['conversation']), 1)
        self.assertNotIn('SYNTHETIC_PRIVATE_',json.dumps(wire))
        followup = terminal(row, answer(wire, 'qa'))
        self.assertEqual(followup['state'], 'completed')
        self.assertEqual(followup['parent_id'], valid_lesson['id'])

        row, wire = submit('synthetic-same-source','lesson')
        rejected = answer(wire, 'lesson')
        refs = wire['sources'][0]['excerpts']
        self.assertNotEqual(refs[0]['evidence_id'], refs[1]['evidence_id'])
        rejected['sections'][3]['citations'] = [{'evidence_id': ref['evidence_id']} for ref in refs[:2]]
        bad_lesson = terminal(row, rejected)
        self.assertEqual(bad_lesson['state'], 'failed')
        self.assertIsNone(bad_lesson['answer'])
        self.assertEqual(service.diagnostics(item, version)[bad_lesson['id']]['code'], 'comparison_sources')
        with self.assertRaisesRegex(StudyError, '同版本的有效教學'):
            service.generate(item, version, 'synthetic-invalid-parent', 'qa', '追問', bad_lesson['id'])
        self.assertEqual(sum(isinstance(call, dict) for call in self.client.calls), 3)

        row, wire = submit('synthetic-input-echo','qa',valid_lesson['id'])
        bad_followup = terminal(row, {
            'mode':wire['mode'], 'prompt':wire['prompt'], 'sources':wire['sources'][:1],
            'conversation':[], 'context_truncated':True, 'limitations':'合成複製輸入。'})
        self.assertEqual(bad_followup['state'], 'failed')
        self.assertIsNone(bad_followup['answer'])
        self.assertEqual(service.diagnostics(item, version)[bad_followup['id']]['code'], 'answer_shape')

        row, wire = submit('synthetic-unsupported-citation','qa',valid_lesson['id'])
        unsupported = answer(wire, 'qa')
        unsupported['sections'][0]['text'] = '模型在每次檢索時都更新全部權重。'
        unsupported['sections'][0]['citations'] = [{'evidence_id':'E-'+'0'*24}]
        rejected_claim = terminal(row, unsupported)
        self.assertEqual(rejected_claim['state'], 'failed')
        self.assertIsNone(rejected_claim['answer'])
        self.assertEqual(service.records(item)[1]['id'], followup['id'])
        self.assertEqual(sum(isinstance(call, dict) for call in self.client.calls), 5)
