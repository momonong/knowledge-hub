"""Host-owned acceptance: real SQLite import, document persistence and safe access."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
try:
    from knowledge_hub.library import KnowledgeLibrary
except ImportError:
    KnowledgeLibrary = None


def seed():
    catalog = {'schema_version': 1, 'domains': [{'id': 'D1', 'title': '知識檢索'}], 'sources': [
        {'id': 'sample-rag', 'title': 'Retrieval Grounding', 'primary_url': 'https://example.org/rag', 'primary_domain': 'D1', 'domains': ['D1'], 'summary_zh': '跨語言檢索與證據定位', 'why_fit_zh': '串聯歷史知識', 'topics': ['retrieval'], 'authors_or_org': 'Example authors', 'year': 2020, 'kind': 'paper', 'read_scope': 'abstract', 'license_status': 'not_assessed', 'corpus_status': 'curated_source_card_only', 'learner_mastery': 'unknown'},
        {'id': 'sample-agent', 'title': 'Agent Memory', 'primary_url': 'https://example.org/agent', 'primary_domain': 'D1', 'domains': ['D1'], 'summary_zh': '長期記憶的可靠性', 'why_fit_zh': '接續學習', 'topics': ['agent'], 'authors_or_org': 'Example authors', 'year': 2024, 'kind': 'paper', 'read_scope': 'abstract', 'license_status': 'not_assessed', 'corpus_status': 'curated_source_card_only', 'learner_mastery': 'unknown'}]}
    graph = {'schema_version': 1, 'concepts': [
        {'id': 'retrieval', 'title': '檢索', 'domain': 'D1', 'summary_zh': '取回證據', 'supporting_source_ids': ['sample-rag'], 'assertion_status': 'curator_synthesis', 'user_mastery': 'unknown'},
        {'id': 'memory', 'title': '記憶', 'domain': 'D1', 'summary_zh': '保留經驗', 'supporting_source_ids': ['sample-agent'], 'assertion_status': 'curator_synthesis', 'user_mastery': 'unknown'}], 'relations': [
        {'source': 'retrieval', 'target': 'memory', 'type': 'helpful_background_for', 'assertion_status': 'curator_proposal', 'evidence_for_underlying_concepts': ['sample-rag', 'sample-agent'], 'note_zh': '建議背景，非論文引用'}]}
    starter = {'items': [{'order': 1, 'source_id': 'sample-rag', 'learning_goal_zh': '理解取回', 'review_question_zh': '證據在哪？', 'user_mastery': 'unknown'}]}
    return catalog, graph, starter


class InitialLibraryAcceptance(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(KnowledgeLibrary, 'Missing initial knowledge library implementation')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'knowledge.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.lib = KnowledgeLibrary(self.store)

    def import_seed(self):
        self.lib.import_seed(*seed())

    def document(self, **kwargs):
        fields = dict(media_type='text/plain', source_url='https://example.org/rag', version_label='snapshot-test', blocks=[{'anchor': 'page-1', 'text': 'A unique grounded passage: 星圖證據 persists across restarts.', 'source_url': 'https://example.org/rag#page=1'}], scope='partial', extraction_note='synthetic acceptance text', rights_note='synthetic')
        fields.update(kwargs)
        return self.lib.add_document('sample-rag', b'A synthetic original document.', **fields)

    def test_import_is_atomic_idempotent_and_does_not_claim_fulltext(self):
        self.import_seed(); self.import_seed()
        self.assertEqual(len(self.lib.list_sources()), 2)
        self.assertEqual(self.lib.stats()['concepts'], 2)
        self.assertEqual(self.lib.stats()['relations'], 1)
        self.assertEqual(self.lib.stats()['documents'], 0)
        self.assertEqual(self.lib.get_source('sample-rag')['documents'], [])
        self.assertEqual(len(self.lib.list_sources(starter_only=True)), 1)
        self.assertEqual(self.store.get_all_items(), [])
        c, g, s = seed(); c['sources'][0]['title'] = 'silently changed'
        with self.assertRaises(ValueError): self.lib.import_seed(c, g, s)
        self.assertEqual(self.lib.get_source('sample-rag')['title'], 'Retrieval Grounding')
        c, g, s = seed(); c['sources'][0]['id'] = 'new-source'
        with self.assertRaises(ValueError): self.lib.import_seed(c, g, s)
        self.assertEqual(len(self.lib.list_sources()), 2)

    def test_document_roundtrip_notes_search_related_and_backup(self):
        self.import_seed(); doc = self.document(); repeated = self.document()
        self.assertEqual(doc['version_id'], repeated['version_id'])
        self.assertEqual(self.lib.stats()['documents'], 1)
        self.store.add_note(doc['item_id'], doc['version_id'], 'page-1', 'PRIVATE acceptance note')
        reopened = KnowledgeLibrary(KnowledgeStore(self.db))
        self.assertTrue(any(x['source_id'] == 'sample-rag' for x in reopened.search('星圖證據')))
        self.assertTrue(any(x['source_id'] == 'sample-rag' for x in reopened.search('跨語言')))
        self.assertTrue(any(x['source_id'] == 'sample-rag' for x in reopened.search('retrieval')))
        for q in ['"', "x' OR 1=1 --", 'NEAR(', '*']:
            self.assertIsInstance(reopened.search(q), list)
        related = reopened.get_source('sample-rag')['related']
        self.assertTrue(any(x['source_id'] == 'sample-agent' and x['assertion_status'] == 'curator_proposal' for x in related))
        raw, mime = reopened.read_blob(doc['blob_sha256'])
        self.assertEqual(raw, b'A synthetic original document.')
        self.assertEqual(mime, 'text/plain')
        item = self.store.get_item(doc['item_id'])
        self.assertEqual(item['notes'][0]['text'], 'PRIVATE acceptance note')
        out = self.root / 'backup.zip'; reopened.backup(out)
        with zipfile.ZipFile(out) as z:
            self.assertIn('knowledge.sqlite3', z.namelist())
            self.assertIn('manifest.json', z.namelist())
            self.assertTrue(any(z.read(n) == raw for n in z.namelist() if n.startswith('objects/')))
        with self.assertRaises((ValueError, FileExistsError)): reopened.backup(out)

    def test_blob_tamper_and_invalid_input_rejected_without_documents(self):
        self.import_seed()
        with self.assertRaises(ValueError): self.document(media_type='application/pdf')
        with self.assertRaises(ValueError): self.document(blocks=[{'anchor': '../evil', 'text': 'bad', 'source_url': 'https://example.org'}])
        with self.assertRaises(ValueError): self.document(source_url='javascript:alert(1)')
        self.assertEqual(self.lib.stats()['documents'], 0)
        doc = self.document()
        blob = self.root / 'library' / 'objects' / doc['blob_sha256'][:2] / doc['blob_sha256']
        self.assertEqual(hashlib.sha256(blob.read_bytes()).hexdigest(), doc['blob_sha256'])
        blob.write_bytes(b'corrupted')
        with self.assertRaises((ValueError, OSError)): self.lib.read_blob(doc['blob_sha256'])
        with self.assertRaises((ValueError, OSError)): self.lib.backup(self.root / 'invalid.zip')
        with self.assertRaises((ValueError, OSError)): self.lib.read_blob('../../knowledge.sqlite3')

    def test_library_routes_search_original_and_export_are_integrated(self):
        self.import_seed(); doc = self.document(); app = create_app(self.db)
        def get(path, query=''):
            response = {}
            def start(status, headers): response.update(status=status, headers=dict(headers))
            payload = b''.join(app({'REQUEST_METHOD': 'GET', 'PATH_INFO': path, 'QUERY_STRING': query, 'HTTP_HOST': '127.0.0.1:8765', 'SERVER_NAME': '127.0.0.1', 'SERVER_PORT': '8765', 'wsgi.url_scheme': 'http', 'wsgi.input': io.BytesIO()}, start))
            return response, payload
        for path in ['/', '/library', '/library/sample-rag', '/search']:
            response, body = get(path, 'q=retrieval' if path == '/search' else '')
            self.assertTrue(response['status'].startswith('200'), path)
            self.assertIn(b'/library', body, path)
        _, body = get('/library/sample-rag')
        self.assertIn(b'/library/sample-agent', body)
        self.assertIn(f'/items/{doc["item_id"]}'.encode(), body)
        response, body = get('/library/files/' + doc['blob_sha256'])
        self.assertEqual(body, b'A synthetic original document.')
        self.assertIn('attachment', response['headers']['Content-Disposition'])
        _, body = get('/export.json')
        self.assertIn('library', json.loads(body))


if __name__ == '__main__': unittest.main()
