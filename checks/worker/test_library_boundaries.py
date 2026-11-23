"""Host-run regressions for immutable library mappings and filesystem boundaries.

These cases are synthetic and do not imply real-source or browser acceptance.
"""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from knowledge_hub.library import KnowledgeLibrary, main
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.web import create_app
from knowledge_hub import library_views


def fixture():
    catalog = {'schema_version': 1, 'domains': [{'id': 'd', 'title': '檢索'}],
               'sources': [{'id': 'a', 'title': 'Library <script>alert(1)</script>',
                   'primary_url': 'https://example.org/a', 'primary_domain': 'd',
                   'domains': ['d'], 'summary_zh': '策展檢索摘要', 'year': 2020,
                   'learner_mastery': 'unknown'},
                   {'id': 'b', 'title': 'Other source', 'primary_url': 'https://example.org/b',
                    'primary_domain': 'd', 'domains': ['d']}]}
    concepts = {'concepts': [{'id': 'c', 'title': 'Shared concept', 'domain': 'd',
                             'supporting_source_ids': ['a', 'b'], 'user_mastery': 'unknown'}],
                'relations': []}
    starter = {'items': [{'order': 1, 'source_id': 'a', 'user_mastery': 'unknown'}]}
    return catalog, concepts, starter


def fields(**extra):
    result = dict(media_type='text/plain', source_url='https://example.org/a',
                  version_label='snapshot', scope='partial', extraction_note='Synthetic extraction',
                  blocks=[{'anchor': 'page-1', 'text': 'Unique page text 定位證據.',
                           'source_url': 'https://example.org/a#page=1'}])
    result.update(extra)
    return result


class SearchExcerptParser(HTMLParser):
    """Read rendered text and collapsed state without counting HTML entities as text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.details = []
        self.excerpts = []
        self.active = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'details':
            self.details.append('open' not in attrs)
        if tag == 'a':
            self.links.append((attrs.get('href', ''), any(self.details)))
        if tag == 'span' and attrs.get('class') == 'search-excerpt':
            self.active = {'text': '', 'hidden': any(self.details)}

    def handle_data(self, data):
        if self.active is not None:
            self.active['text'] += data

    def handle_endtag(self, tag):
        if tag == 'span' and self.active is not None:
            self.excerpts.append(self.active)
            self.active = None
        if tag == 'details':
            self.details.pop()


class LibraryBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = KnowledgeStore(self.root / 'hub.sqlite3')
        self.lib = KnowledgeLibrary(self.store)
        self.lib.import_seed(*fixture())

    def attach(self, **overrides):
        return self.lib.add_document('a', b'synthetic original', **fields(**overrides))

    def test_long_legacy_search_is_bounded_collapsed_and_keeps_full_original(self):
        blocks = [{'anchor': f'page-{i}',
                   'text': ('前言ßİ' * 1000 + '<script>marker()</script> GraphRAG evidence '
                            + '尾文' * 800 + f' FULL-END-{i}'),
                   'source_url': f'https://example.org/a#page={i}'} for i in range(1, 7)]
        doc = self.lib.add_document('a', b'long synthetic original', **fields(blocks=blocks))
        note = 'GraphRAG PRIVATE note stays searchable'
        self.store.add_note(doc['item_id'], doc['version_id'], 'page-1', note)
        before = self.store.export_data()
        original_hits = self.store.search('graphrag')[0]['hits']
        self.assertEqual(len(original_hits), 7)
        self.assertTrue(all(b['text'] in [h['snippet'] for h in original_hits] for b in blocks))
        app = create_app(self.store.db_path)
        def page(path, query=''):
            response = {}
            def start(status, headers):
                response['status'] = status
            body = b''.join(app({'REQUEST_METHOD': 'GET', 'PATH_INFO': path,
                'QUERY_STRING': query, 'HTTP_HOST': '127.0.0.1:8765', 'SERVER_NAME': '127.0.0.1',
                'SERVER_PORT': '8765', 'wsgi.url_scheme': 'http', 'wsgi.input': io.BytesIO()}, start))
            self.assertEqual(response['status'], '200 OK')
            return body.decode('utf-8')
        rendered = page('/search', 'q=graphrag')
        parsed = SearchExcerptParser()
        parsed.feed(rendered)
        self.assertEqual(len(parsed.excerpts), 7)
        self.assertEqual(sum(not hit['hidden'] for hit in parsed.excerpts), 3)
        self.assertEqual(sum(hit['hidden'] for hit in parsed.excerpts), 4)
        self.assertIn('其餘 4 筆符合結果', rendered)
        for hit in parsed.excerpts:
            self.assertLessEqual(len(hit['text']), 320)
            self.assertIn('graphrag', hit['text'].casefold())
        self.assertTrue(any('…' in hit['text'] for hit in parsed.excerpts))
        self.assertIn(note, [hit['text'] for hit in parsed.excerpts])
        self.assertIn('&lt;script&gt;marker()&lt;/script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertLess(rendered.index('搜尋來源與已保存原文'), rendered.index('既有文章、筆記與問答'))
        for block in blocks:
            link = f'/items/{doc["item_id"]}?version={doc["version_id"]}#{block["anchor"]}'
            self.assertTrue(any(href == link for href, hidden in parsed.links))
        last = f'/items/{doc["item_id"]}?version={doc["version_id"]}#page-6'
        self.assertIn((last, True), parsed.links)
        reading = page(f'/items/{doc["item_id"]}', f'version={doc["version_id"]}')
        for block in blocks:
            self.assertIn(html.escape(block['text'], quote=True), reading)
        self.assertEqual(self.store.search('graphrag')[0]['hits'], original_hits)
        self.assertEqual(self.store.export_data(), before)
        self.assertEqual(before['items'][0]['versions'][0]['blocks'], blocks)

    def test_named_v1_formats_preserve_metadata_and_are_idempotent(self):
        seed = fixture()
        seed[0]['schema_version'] = 'knowledge-hub-research-seed-v1'
        seed[1]['schema_version'] = 'curated-concepts-v1'
        original = copy.deepcopy(seed)
        lib = KnowledgeLibrary(KnowledgeStore(self.root / 'named.sqlite3'))
        first = lib.import_seed(*seed)
        before = lib.export_metadata()
        self.assertEqual(lib.import_seed(*seed), first)
        self.assertEqual(lib.export_metadata(), before)
        self.assertEqual(seed, original)
        self.assertEqual(before['metadata']['catalog']['schema_version'],
                         'knowledge-hub-research-seed-v1')
        self.assertEqual(before['metadata']['concept_map']['schema_version'],
                         'curated-concepts-v1')
        self.assertNotIn('schema_version', before['metadata']['starter'])
        self.assertEqual(first['sources'], 2)
        self.assertEqual(first['concepts'], 1)
        self.assertEqual(lib.store.get_all_items(), [])

    def test_integer_and_absent_versions_remain_supported_for_each_role(self):
        for case, with_version in enumerate((False, True)):
            with self.subTest(with_version=with_version):
                seed = fixture()
                for document in seed:
                    document.pop('schema_version', None)
                    if with_version:
                        document['schema_version'] = 1
                lib = KnowledgeLibrary(KnowledgeStore(self.root / f'compatible-{case}.sqlite3'))
                lib.import_seed(*seed)
                for role in ('catalog', 'concept_map', 'starter'):
                    metadata = lib.export_metadata()['metadata'][role]
                    if with_version:
                        self.assertIs(type(metadata['schema_version']), int)
                        self.assertEqual(metadata['schema_version'], 1)
                    else:
                        self.assertNotIn('schema_version', metadata)

    def test_unknown_wrong_role_and_malformed_versions_fail_without_mutation(self):
        doc = self.attach()
        self.store.add_note(doc['item_id'], doc['version_id'], 'page-1', 'PRIVATE')
        before = self.store.export_data()
        before_hits = self.lib.search('策展')
        named = ('knowledge-hub-research-seed-v1', 'curated-concepts-v1')
        for index, role in enumerate(('catalog', 'concept_map', 'starter')):
            invalid = [True, False, None, 1.0, 0, 2, '1', '', 'unknown-v1',
                       'knowledge-hub-research-seed-v2', [], {}, {'version': 1}]
            invalid.extend(tag for tag in named if index == 2 or tag != named[index])
            for version in invalid:
                with self.subTest(role=role, version=version):
                    seed = fixture()
                    seed[0]['domains'].insert(0, {'id': 'new', 'title': 'Must not be saved'})
                    seed[index]['schema_version'] = version
                    with self.assertRaises(ValueError):
                        self.lib.import_seed(*seed)
                    self.assertEqual(self.store.export_data(), before)
                    self.assertEqual(self.lib.search('策展'), before_hits)

    def test_learner_labels_and_closed_audit_keep_original_data(self):
        self.attach()
        source = self.lib.get_source('a')
        source.update(read_scope='overview', license_status='not_assessed',
                      corpus_status='curated_source_card_only', assertion_status='curator_proposal')
        source['concepts'][0]['assertion_status'] = 'curator_synthesis'
        before = copy.deepcopy(source)
        page = library_views.detail(source)
        for translated in ('內容概覽', '尚未評估', '僅有策展來源卡', '策展整理', '策展建議',
                           '掌握度記錄：未知', '已擷取部分文字，圖表與公式待核對',
                           '尚未核對為論文引用或技術繼承關係。'):
            self.assertIn(translated, page)
        for raw in ('curator_proposal', 'curator_synthesis', 'not_assessed',
                    'curated_source_card_only', '>overview<', '>unknown<'):
            self.assertNotIn(raw, page)
        audit_start = page.index('<details><summary>來源與擷取稽核資訊</summary>')
        audit_end = page.index('</details>', audit_start)
        self.assertLess(page.index('下載原始檔案'), audit_start)
        self.assertLess(page.index('也不代表已逐頁核對'), audit_start)
        for value in ('SHA256', source['documents'][0]['blob_sha256'],
                      source['documents'][0]['acquired_at'], 'Synthetic extraction',
                      source['documents'][0]['version_label'], '權利說明與依據'):
            self.assertIn(value, page[audit_start:audit_end])
        self.assertNotIn('<details open', page)
        self.assertEqual(source, before)
        for scope, expected in (('abstract', '僅摘要'),
                                ('full', '已擷取全文文字，圖表與公式待核對')):
            source['documents'][0]['scope'] = scope
            self.assertIn(expected, library_views.detail(source))
        source['read_scope'] = '<unknown-scope>'
        source['documents'][0]['scope'] = '<unknown-scope>'
        source['concepts'][0]['user_mastery'] = '<unknown-mastery>'
        unknown_page = library_views.detail(source)
        self.assertIn('&lt;unknown-scope&gt;', unknown_page)
        self.assertIn('&lt;unknown-mastery&gt;', unknown_page)
        self.assertNotIn('<unknown-scope>', unknown_page)
        listing = library_views.listing(self.lib)
        self.assertIn('優先閱讀清單', listing)
        self.assertIn('搜尋來源與已保存原文', listing)
        self.assertNotIn('只看 starter', listing)
        self.assertNotIn('12 篇', listing)
        results = library_views.hits(self.lib.search('策展'))
        self.assertIn('<details><summary>搜尋方式</summary>', results)
        self.assertGreater(results.index('SQLite FTS5'), results.index('<details>'))
        self.assertLess(results.index('非向量搜尋'), results.index('</details>'))

    def test_same_bytes_on_different_sources_have_independent_mappings(self):
        original = self.attach()
        independent = self.lib.add_document('b', b'synthetic original', **fields(
            version_label='independent-version', source_url='https://example.org/b',
            blocks=[{'anchor': 'different-page', 'text': 'Independent text mapping',
                     'source_url': 'https://example.org/b#page=2'}]))
        self.assertEqual(original['blob_sha256'], independent['blob_sha256'])
        self.assertNotEqual(original['item_id'], independent['item_id'])
        self.assertEqual(self.lib.get_source('a')['documents'][0]['version_label'], 'snapshot')
        self.assertEqual(self.lib.get_source('b')['documents'][0]['version_label'], 'independent-version')
        self.assertEqual(self.lib.stats()['documents'], 2)
        self.assertEqual(len(list(self.lib.objects_dir.glob('*/*'))), 1)

    def test_conflict_rolls_back_prior_new_rows_and_preserves_notes(self):
        doc = self.attach()
        self.store.add_note(doc['item_id'], doc['version_id'], 'page-1', 'PRIVATE')
        catalog, graph, starter = fixture()
        catalog['domains'].insert(0, {'id': 'new-domain', 'title': 'New domain'})
        catalog['sources'][0]['learner_mastery'] = 'mastered'
        with self.assertRaises(ValueError):
            self.lib.import_seed(catalog, graph, starter)
        self.assertEqual([d['id'] for d in self.lib.domains()], ['d'])
        self.assertEqual(self.lib.get_source('a')['learner_mastery'], 'unknown')
        self.assertEqual(self.store.get_item(doc['item_id'])['notes'][0]['text'], 'PRIVATE')

    def test_same_bytes_cannot_change_mapping_or_version(self):
        doc = self.attach()
        for change in ({'version_label': 'v2'}, {'source_url': 'https://example.org/other'},
                       {'blocks': [{'anchor': 'page-1', 'text': 'changed', 'source_url': 'https://example.org/a'}]}):
            with self.assertRaises(ValueError):
                self.attach(**change)
        self.assertEqual(self.lib.stats()['documents'], 1)
        self.assertEqual(len(self.store.get_item(doc['item_id'])['versions']), 1)

    def test_invalid_blocks_are_rejected_before_files_or_versions(self):
        for blocks in ([], [{'anchor': '../x', 'text': 'text', 'source_url': 'https://example.org'}],
                       fields()['blocks'] * 2,
                       [{'anchor': 'p', 'text': '字' * 800000, 'source_url': 'https://example.org'}]):
            with self.assertRaises(ValueError):
                self.attach(blocks=blocks)
        self.assertEqual(self.store.get_all_items(), [])
        self.assertFalse(self.lib.objects_dir.exists())

    def test_symlink_directory_and_blob_are_never_followed(self):
        target = self.root / 'elsewhere'
        target.mkdir()
        library_root = self.root / 'library'
        library_root.mkdir()
        self.lib.objects_dir.symlink_to(target, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.attach()
        self.assertEqual(list(target.iterdir()), [])
        self.lib.objects_dir.unlink()
        doc = self.attach()
        blob = self.lib.objects_dir / doc['blob_sha256'][:2] / doc['blob_sha256']
        outside = self.root / 'outside.txt'
        outside.write_bytes(blob.read_bytes())
        blob.unlink()
        blob.symlink_to(outside)
        with self.assertRaises((ValueError, OSError)):
            self.lib.read_blob(doc['blob_sha256'])
        with self.assertRaises((ValueError, OSError)):
            self.lib.backup(self.root / 'bad.zip')
        self.assertFalse((self.root / 'bad.zip').exists())

    def test_backup_snapshot_preserves_private_data_and_hash_manifest(self):
        doc = self.attach()
        self.store.add_note(doc['item_id'], doc['version_id'], 'page-1', 'PRIVATE')
        output = self.root / 'backup.zip'
        self.lib.backup(output)
        with zipfile.ZipFile(output) as z:
            manifest = json.loads(z.read('manifest.json'))
            for entry in manifest['files']:
                self.assertEqual(hashlib.sha256(z.read(entry['path'])).hexdigest(), entry['sha256'])
            restored = self.root / 'restored'
            restored.mkdir()
            (restored / 'knowledge.sqlite3').write_bytes(z.read('knowledge.sqlite3'))
            for entry in manifest['files']:
                if entry['path'].startswith('objects/'):
                    path = restored / 'library' / entry['path']
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(z.read(entry['path']))
        store = KnowledgeStore(restored / 'knowledge.sqlite3')
        lib = KnowledgeLibrary(store)
        self.assertEqual(store.get_item(doc['item_id'])['notes'][0]['text'], 'PRIVATE')
        self.assertEqual(lib.read_blob(doc['blob_sha256'])[0], b'synthetic original')

    def test_arxiv_original_reuses_canonical_item_and_keeps_old_version(self):
        catalog, graph, starter = fixture()
        catalog['sources'][0]['primary_url'] = 'https://arxiv.org/abs/2001.00001v2'
        fresh = KnowledgeLibrary(KnowledgeStore(self.root / 'second.sqlite3'))
        fresh.import_seed(catalog, graph, starter)
        old, old_version, _ = fresh.store.upsert_parsed_entry(dict(key='arxiv:2001.00001',
            source_name='arxiv', title='Existing title', label='v1', scope='abstract',
            source_url='https://arxiv.org/abs/2001.00001v1', content_hash='old',
            blocks=[{'anchor': 'abstract', 'text': 'Existing abstract', 'source_url': 'https://arxiv.org/abs/2001.00001v1'}]))
        fresh.store.set_item_status(old, 'later')
        doc = fresh.add_document('a', b'original', **fields(version_label='v2'))
        self.assertEqual(doc['item_id'], old)
        item = fresh.store.get_item(old)
        self.assertEqual(item['status'], 'later')
        self.assertEqual(len(item['versions']), 2)
        self.assertTrue(any(str(v['id']) == str(old_version) for v in item['versions']))

    def test_html_original_is_attachment_and_card_text_is_escaped(self):
        raw = b'<script>alert(1)</script>'
        doc = self.lib.add_document('a', raw, **fields(media_type='text/html'))
        app = create_app(self.store.db_path)
        def request(path, query=''):
            response = {}
            def start(status, headers):
                response.update(status=status, headers=dict(headers))
            payload = b''.join(app({'REQUEST_METHOD': 'GET', 'PATH_INFO': path,
                'QUERY_STRING': query, 'HTTP_HOST': '127.0.0.1:8765', 'SERVER_NAME': '127.0.0.1',
                'SERVER_PORT': '8765', 'wsgi.url_scheme': 'http', 'wsgi.input': io.BytesIO()}, start))
            return response, payload
        _, card = request('/library/a')
        self.assertIn(b'&lt;script&gt;', card)
        self.assertNotIn(b'<script>', card)
        response, payload = request('/library/files/' + doc['blob_sha256'])
        self.assertEqual(payload, raw)
        self.assertEqual(response['headers']['X-Content-Type-Options'], 'nosniff')
        self.assertIn('attachment', response['headers']['Content-Disposition'])
        _, page = request(f'/items/{doc["item_id"]}')
        self.assertIn(b'/library/a', page)
        self.assertEqual(request('/library', 'q=' + 'x' * 2001)[0]['status'], '400 Bad Request')

    def test_manifest_validates_last_entry_and_paths_before_first_attachment(self):
        raw = self.root / 'original.txt'
        raw.write_bytes(b'first original')
        manifest = self.root / 'manifest.json'
        entries = [dict(source_id='a', blob_path=raw.name, **fields()),
                   dict(source_id='b', blob_path=raw.name, **fields(media_type='application/pdf'))]
        manifest.write_text(json.dumps({'documents': entries}), encoding='utf-8')
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(['--db', str(self.store.db_path), 'attach', '--manifest', str(manifest)])
        self.assertNotEqual(error.exception.code, 0)
        self.assertEqual(self.lib.stats()['documents'], 0)
        entries[1] = dict(source_id='b', blob_path='../outside.txt', **fields())
        manifest.write_text(json.dumps({'documents': entries}), encoding='utf-8')
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--db', str(self.store.db_path), 'attach', '--manifest', str(manifest)])
        self.assertEqual(self.lib.stats()['documents'], 0)


if __name__ == '__main__':
    unittest.main()
