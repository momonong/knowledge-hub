"""Keep the actual failure boundary across SDK conversion and SQLite restarts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from test_project_broker import Client
from test_study import entry, FakeProvider
from knowledge_hub.project_broker import ProjectStudyProvider
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.study import StudyService, encoded


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        root.chmod(0o700)
        token = root/'token'
        token.write_text('synthetic-project-token-not-real-123456')
        token.chmod(0o600)
        profile = root/'profile.json'
        profile.write_text('{"fixture":"public-root"}')
        self.provider = ProjectStudyProvider('https://momonong.me/quota/api/v1',token,
            'google','gemini-3.5-flash-lite',trust_profile=profile,trust_state=root/'trust.sqlite',
            max_input_bytes=32768,max_output_tokens=8192,max_operations=3,
            session_id='trace-test',client_factory=Client)
        self.db = root/'kh.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.item,self.version,_ = self.store.upsert_parsed_entry(entry('arxiv:2601.00001',
            'Memory retrieval','A memory system retrieves relevant observations before generation.'))
        self.store.add_note(self.item,self.version,'p1','PRIVATE_NOTE_SENTINEL')
        self.service = StudyService(self.store,self.provider)

    def test_original_unknown_to_failed_trace_survives_restart_without_replay(self):
        row = self.service.generate(self.item,self.version,'trace-1','lesson','Explain the method.')
        client = self.provider._client
        client.reply = {'state':'unknown','http_status':503,'attempt_count':1,
                        'error_code':'provider_http_error','message':'SECRET_ERROR_SENTINEL',
                        'diagnostics':{'phase':'parse_response','exception_type':'ValueError',
                                       'message':'SECRET_ERROR_SENTINEL'}}
        self.service.reconcile(row['id'])
        client.reply = {'state':'failed','http_status':503,'attempt_count':1,'result_available':True}
        client.outcome = {'state':'failed','http_status':503,'error_code':'provider_http_error',
                          'usage_source':'observed_http_request','ledger_state':'failed',
                          'reported_resources':{'requests':1},'reported_input_tokens':None,
                          'diagnostics':{'response_complete':True,'terminal_error_response':True}}
        failed = self.service.reconcile(row['id'])
        service = StudyService(KnowledgeStore(self.db),self.provider)
        events = service.events(row['id'])
        self.assertEqual([e['event'] for e in events],['prepared','submit','poll','poll'])
        self.assertEqual(events[0]['details']['request_sha256'],hashlib.sha256(encoded(row['payload']).encode()).hexdigest())
        self.assertEqual(len(events[0]['details']['implementation_sha256']),64)
        self.assertEqual(events[2]['details']['broker_status']['http_status'],503)
        self.assertEqual(events[2]['details']['broker_status']['phase'],'parse_response')
        self.assertEqual(events[-1]['details']['broker_result']['reported_resources'],{'requests':1})
        self.assertIsNone(events[-1]['details']['broker_result']['reported_input_tokens'])
        self.assertEqual(events[-1]['details']['state'],'failed')
        self.assertEqual(failed['reason_code'],'provider_http_failed')
        serialized=json.dumps(events)
        for forbidden in ('PRIVATE_NOTE_SENTINEL','SECRET_ERROR_SENTINEL','synthetic-project-token','Explain the method.'):
            self.assertNotIn(forbidden,serialized)
        self.assertEqual(sum(isinstance(c,dict) for c in client.calls),1)
        self.assertEqual(service.availability()['used'],1)
        before=list(events)
        service.reconcile(row['id'])
        self.assertEqual(service.events(row['id']),before)

    def test_model_completion_is_distinct_from_citation_validation_failure(self):
        fake=FakeProvider(mode='bad_citation',budget=3)
        service=StudyService(self.store,fake)
        row=service.generate(self.item,self.version,'bad-cite','lesson','Teach.')
        event=service.events(row['id'])[-1]['details']
        self.assertEqual(event['provider_state'],'completed')
        self.assertEqual(event['state'],'failed')
        self.assertEqual(event['validation_code'],'citation_not_in_sent_evidence')
        self.assertRegex(event['output_sha256'],r'^[0-9a-f]{64}$')
        self.assertNotIn('THIS TEXT WAS NEVER',json.dumps(event))

    def test_malformed_provider_metadata_does_not_enter_trace_or_clear_unknown(self):
        row=self.service.generate(self.item,self.version,'malformed','lesson','Explain.')
        self.provider._client.reply={'state':'unknown','http_status':True,'attempt_count':'SECRET',
            'error_code':'SECRET','diagnostics':{'phase':'SECRET','response_complete':'SECRET'},
            'provider_request_id':'SECRET','headers':{'Authorization':'SECRET'}}
        saved=self.service.reconcile(row['id'])
        event=self.service.events(row['id'])[-1]['details']
        self.assertEqual(saved['state'],'unknown')
        self.assertNotIn('http_status',event['broker_status'])
        self.assertNotIn('SECRET',json.dumps(event))


if __name__=='__main__':unittest.main()
