"""L4 stream coordination with a real database and deterministic boundaries."""
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from knowledge_hub.followup import Followups
from knowledge_hub.grounding import cached_guide
from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.store import KnowledgeStore
from checks.test_initial_library import seed
from checks.test_study import entry
from checks.test_phase1_grounding import HTML, METHOD, PAPER
from knowledge_hub import visuals
from knowledge_hub.sources import parse_arxiv_html


KINDS = ('evidence', 'limits', 'inference')
QUESTION = '這篇怎麼保存每輪的證據？'
CATALOG = {
    'source-evidence': {
        'anchor': 'details',
        'quote': 'The system stores version-bound events.',
        'href': '/items/1?version=1#details',
        'source_url': 'https://arxiv.org/html/2611.60001v1#details',
    },
}


def raw_sections():
    return [
        {'kind': 'evidence', 'text': '依版本保存事件。', 'ids': ['source-evidence']},
        {'kind': 'limits', 'text': '原文未交代', 'ids': []},
        {'kind': 'inference', 'text': '原文未交代', 'ids': []},
    ]


def checked(section, catalog):
    text = section['text']
    if not section['ids']:
        if text not in ('原文未交代', '原文未交代。'):
            raise ValueError('Missing source')
        text = '原文未交代'
    return {
        'kind': section['kind'], 'text': text,
        'citations': [{'id': key, **catalog[key]} for key in section['ids']],
        'missing': not section['ids'],
    }


class TransportError(Exception):
    def __init__(self, code):
        super().__init__('Provider secret must stay private')
        self.code = code


class FollowupStreamTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'knowledge.db'
        self.store = KnowledgeStore(self.db)
        item_id, version_id, _ = self.store.upsert_parsed_entry(entry(
            'arxiv:2611.60001', 'Immutable Reading', CATALOG['source-evidence']['quote']))
        self.item = self.store.get_item(item_id)
        self.version = self.store.select_version(self.item['versions'], version_id)
        self.item['notes'] = [{'text': 'SECRET_DRAFT'}]
        self.primary, self.fallback = object(), object()
        self.routes = {'primary': self.primary, 'fallback': self.fallback}
        grounding = ModuleType('knowledge_hub.grounding')
        grounding.make_catalog = Mock(return_value=CATALOG)
        grounding.check_section = checked
        grounding.local_sections = Mock(return_value=raw_sections())
        transport = ModuleType('knowledge_hub.model_stream')
        transport.ModelStreamError = TransportError
        transport.stream_model = Mock(side_effect=lambda *a, **k: map(json.dumps, raw_sections()))
        self.grounding, self.transport = grounding, transport
        patcher = patch.dict(sys.modules, {grounding.__name__: grounding, transport.__name__: transport})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_stream(self, service, request_id='turn-1', **changes):
        args = {
            'item': self.item, 'version': self.version,
            'question': QUESTION, 'anchor': 'details', 'request_id': request_id,
        }
        return service.run(**(args | changes))

    def test_local_stream_persists_three_valid_sections_and_discloses_source_mode(self):
        service = Followups(self.store)
        events = list(self.run_stream(service))
        self.assertEqual([event for event, _ in events], ['meta', 'section', 'section', 'section', 'done'])
        self.assertEqual(events[0][1]['mode'], 'local')
        self.assertEqual([data['kind'] for event, data in events if event == 'section'], list(KINDS))
        self.transport.stream_model.assert_not_called()
        reopened = Followups(KnowledgeStore(self.db))
        stored = reopened.history(self.item['id'], self.version['id'])
        self.assertEqual(len(stored), 1)
        self.assertEqual(reopened.get(stored[0]['id']), stored[0])
        self.assertEqual(stored[0]['sections'][0]['citations'][0]['href'], CATALOG['source-evidence']['href'])
        self.assertEqual((stored[0]['question'], stored[0]['anchor'], stored[0]['state']),
                         (QUESTION, 'details', 'completed'))

    def test_model_receives_only_current_question_and_public_quotes(self):
        service = Followups(self.store, self.routes)
        events = list(self.run_stream(service))
        self.assertEqual(events[0][1]['mode'], 'model')
        provider, messages = self.transport.stream_model.call_args.args
        self.assertIs(provider, self.primary)
        deadlines = self.transport.stream_model.call_args.kwargs
        self.assertTrue(0 < deadlines['first_timeout'] <= 15)
        self.assertAlmostEqual(deadlines['total_timeout'] - deadlines['first_timeout'], 30)
        self.assertEqual(json.loads(messages[-1]['content']),
                         {'question': QUESTION, 'evidence': [
                             {'id': 'source-evidence', 'quote': CATALOG['source-evidence']['quote']}
                         ]})
        self.assertNotIn('SECRET_DRAFT', str(messages))

    def test_grounding_loads_stored_edition_instead_of_request_object(self):
        service = Followups(self.store, self.routes)
        spoofed = {**self.version, 'blocks': [
            {'anchor': 'details', 'text': 'FORGED_PRIVATE_QUOTE'}
        ]}
        self.assertEqual(list(self.run_stream(service, version=spoofed))[-1][0], 'done')
        item, version, *_ = self.grounding.make_catalog.call_args.args
        self.assertNotIn('notes', item)
        self.assertEqual(version['blocks'], self.version['blocks'])

    def test_private_attachment_cannot_send_even_with_same_publisher_edition_url(self):
        library = KnowledgeLibrary(self.store)
        catalog, graph, starter = seed()
        catalog['sources'][0]['primary_url'] = self.version['source_url']
        library.import_seed(catalog, graph, starter)
        attached = library.add_document(
            'sample-rag', b'PRIVATE_ATTACHMENT', media_type='text/plain',
            source_url=self.version['source_url'], version_label=self.version['label'],
            blocks=[{'anchor': 'p1', 'text': 'PRIVATE_ATTACHMENT', 'source_url': self.version['source_url']}],
        )
        self.assertEqual(attached['item_id'], self.item['id'])
        public_item = self.store.get_item(attached['item_id'])
        uploaded = self.store.select_version(public_item['versions'], str(attached['version_id']))
        service = Followups(self.store, self.routes)
        blocked = list(self.run_stream(service, version=uploaded))
        self.assertEqual(blocked[-1][1]['reason'], 'source_not_public')
        self.transport.stream_model.assert_not_called()
        self.grounding.make_catalog.assert_not_called()
        self.assertEqual(list(self.run_stream(service, 'published-original'))[-1][0], 'done')
        self.transport.stream_model.assert_called_once()

    def test_model_requires_publisher_url_bound_to_the_exact_edition(self):
        row = entry(self.item['key'], 'A different edition', 'Other blocks')
        row['label'] = 'v2'
        item_id, version_id, _ = self.store.upsert_parsed_entry(row)
        item = self.store.get_item(item_id)
        version = self.store.select_version(item['versions'], version_id)
        events = list(self.run_stream(Followups(self.store, self.routes), version=version))
        self.assertEqual(events[-1][1]['reason'], 'source_not_public')
        self.transport.stream_model.assert_not_called()

    def test_grounding_canonicalizes_valid_missing_text(self):
        service = Followups(self.store, self.routes)
        raw = raw_sections()
        raw[1]['text'] += '。'
        self.transport.stream_model.side_effect = lambda *a, **k: map(json.dumps, raw)
        events = list(self.run_stream(service))
        self.assertEqual(events[2][1]['text'], '原文未交代')
        self.assertEqual(events[-1][0], 'done')

    def test_retry_of_same_intent_replays_and_changed_route_conflicts(self):
        service = Followups(self.store, self.routes)
        first = list(self.run_stream(service))
        reopened = Followups(KnowledgeStore(self.db), self.routes)
        again = list(self.run_stream(reopened))
        self.assertTrue(again[0][1]['replayed'])
        self.assertEqual(again[1:], first[1:])
        self.transport.stream_model.assert_called_once()

        conflict = list(self.run_stream(reopened, route='fallback'))
        self.assertEqual(conflict[-1][1]['reason'], 'request_conflict')
        self.transport.stream_model.assert_called_once()

    def test_inflight_duplicate_observes_reservation_without_sending(self):
        owner = self.run_stream(Followups(self.store, self.routes))
        _, meta = next(owner)
        self.addCleanup(owner.close)
        peer = Followups(KnowledgeStore(self.db), self.routes)
        duplicate = list(self.run_stream(peer))
        self.assertEqual((duplicate[0][1]['id'], duplicate[-1][1]['reason']), (meta['id'], 'running'))
        self.transport.stream_model.assert_not_called()
        self.assertEqual(list(owner)[-1][0], 'done')
        self.transport.stream_model.assert_called_once()

    def test_expired_reservation_converges_without_resending_on_read(self):
        service = Followups(self.store, self.routes)
        with patch('knowledge_hub.followup.time') as clock:
            clock.return_value = 200
            owner = self.run_stream(service)
            _, meta = next(owner)
            self.addCleanup(owner.close)
            clock.return_value = 300
            reopened = Followups(KnowledgeStore(self.db), self.routes)
            abandoned = reopened.get(meta['id'])
            self.assertEqual((abandoned['state'], abandoned['reason']), ('failed', 'interrupted'))
            self.assertEqual(list(self.run_stream(reopened))[-1][1]['reason'], 'interrupted')
            self.assertEqual(list(owner)[-1][1]['reason'], 'interrupted')
        self.transport.stream_model.assert_not_called()

    def test_invalid_citation_and_malformed_sequence_fail_before_emission(self):
        service = Followups(self.store, self.routes)
        cases = {
            'invalid_citation': {'kind': 'evidence', 'text': '偽造來源。', 'ids': ['fake-source']},
            'invalid_response': {'kind': 'limits', 'text': '原文未交代', 'ids': []},
        }
        for reason, invalid in cases.items():
            with self.subTest(reason=reason):
                self.transport.stream_model.side_effect = lambda *a, **k: map(json.dumps, [invalid])
                events = list(self.run_stream(service, reason))
                self.assertEqual([event for event, _ in events], ['meta', 'error'])
                self.assertEqual(events[-1][1]['reason'], reason)
                row = service.get(events[0][1]['id'])
                self.assertEqual((row['state'], row['reason'], row['sections']), ('failed', reason, []))

    def test_timeout_is_visible_and_fallback_requires_its_own_request(self):
        service = Followups(self.store, self.routes)

        def answer(config, *args, **kwargs):
            if config is self.primary:
                raise TransportError('first_token_timeout')
            yield from map(json.dumps, raw_sections())

        self.transport.stream_model.side_effect = answer
        first = list(self.run_stream(service))
        self.assertEqual(first[-1][1]['reason'], 'first_token_timeout')
        self.assertTrue(first[-1][1]['fallback_available'])
        self.assertNotIn('Provider secret', str(first))
        self.transport.stream_model.assert_called_once()
        switched = list(self.run_stream(service, 'turn-2', route='fallback'))
        self.assertEqual((switched[0][1]['route'], switched[-1][0]), ('fallback', 'done'))
        self.assertIs(self.transport.stream_model.call_args.args[0], self.fallback)

    def test_unconfigured_fallback_fails_with_its_own_persisted_intent(self):
        service = Followups(self.store)
        events = list(self.run_stream(service, route='fallback'))
        self.assertEqual(events[-1][1]['reason'], 'route_unavailable')
        self.assertEqual(service.get(events[0][1]['id'])['route'], 'fallback')
        self.grounding.local_sections.assert_not_called()
        self.transport.stream_model.assert_not_called()

    def test_client_close_terminates_transport_and_keeps_validated_progress(self):
        service = Followups(self.store, self.routes)
        transport_closed = []

        def answer(*args, **kwargs):
            try:
                yield from map(json.dumps, raw_sections())
            finally:
                transport_closed.append(True)

        self.transport.stream_model.side_effect = answer
        stream = self.run_stream(service)
        self.addCleanup(stream.close)
        _, meta = next(stream)
        self.assertEqual(next(stream)[0], 'section')
        stream.close()
        record = service.get(meta['id'])
        self.assertEqual(transport_closed, [True])
        self.assertEqual((record['state'], record['reason']), ('failed', 'interrupted'))
        self.assertEqual([section['kind'] for section in record['sections']], ['evidence'])
        self.assertEqual(list(self.run_stream(service))[-1][1]['reason'], 'interrupted')
        self.transport.stream_model.assert_called_once()


class SelectedFollowupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'knowledge.db'
        self.store = KnowledgeStore(self.db)
        item_id, version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(HTML,PAPER))
        self.item = self.store.get_item(item_id)
        self.version = self.store.select_version(self.item['versions'],version_id)
        self.request_messages = []
        transport = ModuleType('knowledge_hub.model_stream')
        transport.ModelStreamError = TransportError
        transport.stream_model = self.answer
        patcher = patch.dict(sys.modules,{transport.__name__:transport})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = Followups(self.store,{'primary':object(),'fallback':object()})

    def answer(self, config, messages, **kwargs):
        sent = json.loads(messages[-1]['content'])
        self.request_messages.append(sent)
        sections = raw_sections()
        sections[0]['ids'] = [sent['evidence'][0]['id']]
        yield from map(json.dumps,sections)

    def send(self, request_id, anchor, quote):
        return list(self.service.run(self.item,self.version,QUESTION,anchor,request_id,quote=quote))

    def test_exact_body_selection_reaches_model_as_first_evidence_and_identifies_intent(self):
        selected = 'compresses the stored documents'
        events = self.send('body-positive','p1',selected)
        self.assertEqual(events[-1][0],'done')
        first = self.request_messages[0]['evidence'][0]
        self.assertEqual(first['quote'],selected)
        self.assertEqual(events[1][1]['citations'][0]['quote'],selected)
        turn_id = events[0][1]['id']
        self.assertEqual(self.service.get(turn_id)['quote'],selected)
        self.assertTrue(self.send('body-positive','p1',selected)[0][1]['replayed'])
        self.assertEqual(self.send('body-positive','p1','surviving candidates')[-1][1]['reason'],'request_conflict')
        self.assertEqual(len(self.request_messages),1)

    def test_wrong_anchor_or_private_selection_never_reaches_the_provider(self):
        for name, selection in [('wrong-anchor','compresses the stored documents'),('forged','PRIVATE_UNVERIFIED_SELECTION')]:
            with self.subTest(name=name):
                event, data = self.send(name,'p2',selection)[-1]
                self.assertEqual((event,data['reason'],data['fallback_available']),('error','invalid_selection',False))
        self.assertFalse(self.request_messages)

    def test_saved_guide_selection_maps_back_to_publisher_text(self):
        guide = cached_guide(self.store,self.item,self.version)
        section = next(section for section in guide['sections'] if '機制' in section['title'])
        localized = '這是作者提出本篇做法的句子。'
        self.assertIn(localized,section['text'])
        self.assertEqual(self.send('guide-positive','p1',localized)[-1][0],'done')
        first = self.request_messages[0]['evidence'][0]
        self.assertEqual(first['quote'],METHOD)
        self.assertNotIn(localized,json.dumps(self.request_messages,ensure_ascii=False))

    def test_stored_figure_provenance_is_required_even_if_caller_passes_a_manifest(self):
        manifest = visuals.extract(HTML,PAPER)
        supplied = list(self.service.run(self.item,self.version,QUESTION,'S3.T1','untrusted-visual',
                                         panels=manifest,quote='CoralBench recall'))
        self.assertEqual(supplied[-1][1]['reason'],'invalid_selection')
        self.assertFalse(self.request_messages)
        with self.store._connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS version_visuals (version_id INTEGER PRIMARY KEY, manifest TEXT NOT NULL)')
            conn.execute('INSERT INTO version_visuals VALUES (?,?)',(self.version['id'],json.dumps(manifest)))
        self.assertEqual(self.send('stored-visual','S3.T1','CoralBench recall')[-1][0],'done')
        self.assertEqual(self.request_messages[0]['evidence'][0]['quote'],'CoralBench recall')

    def test_legacy_turn_without_quote_still_replays_after_migration(self):
        first = self.send('old-schema','p1','')
        self.assertEqual(first[-1][0],'done')
        with self.store._connect() as conn:
            conn.execute('ALTER TABLE followup_turns DROP COLUMN quote')
        reopened = Followups(KnowledgeStore(self.db),{'primary':object()})
        turn_id = first[0][1]['id']
        self.assertEqual(reopened.get(turn_id)['quote'],'')
        replayed = list(reopened.run(self.item,self.version,QUESTION,'p1','old-schema'))
        self.assertTrue(replayed[0][1]['replayed'])
        self.assertEqual(replayed[1:],first[1:])
        self.assertEqual(len(self.request_messages),1)


if __name__ == '__main__':
    unittest.main()
