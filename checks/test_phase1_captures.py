from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from knowledge_hub import visuals
from knowledge_hub.captures import CaptureError, CaptureStore
from knowledge_hub.editions import ArticleEditions
from knowledge_hub.followup import Followups
from knowledge_hub.grounding import cached_guide, make_catalog
from knowledge_hub.reading import ReadingState
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.store import KnowledgeStore


URL = 'https://arxiv.org/html/2611.01234v1'
PARAGRAPH = 'We propose a retrieval method that compresses stored documents and verifies candidates.'
LIMITS = 'Our evaluation is limited to static collections and does not cover online updates.'
ARTICLE = f"""<article><h1>Fixed Retrieval Study</h1>
<h2 id="method">2 Method</h2><p id="p1">{PARAGRAPH}</p>
<figure id="S3.T1" class="ltx_table"><figcaption>Table 1: Separate benchmark results.</figcaption>
<table class="ltx_tabular"><tr><th>Method</th><th>Recall</th></tr>
<tr><td>Trial</td><td>71%</td></tr></table></figure>
<h2 id="limits">3 Limitations</h2><p id="p2">{LIMITS}</p></article>""".encode()
QUESTION = '索引重建是否涵蓋長時間斷線？'


class Phase1CaptureTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'knowledge.sqlite3'
        self.store = KnowledgeStore(self.db)
        self.followups = Followups(self.store)
        self.captures = CaptureStore(self.store, self.followups)
        self.item_id, self.version_id, _ = self.store.upsert_parsed_entry(parse_arxiv_html(ARTICLE, URL))
        _, self.next_version_id, _ = self.store.upsert_parsed_entry(
            parse_arxiv_html(ARTICLE.replace(b'verifies candidates', b'scores examples'), URL[:-1] + '2'))
        self.item = self.store.get_item(self.item_id)
        self.version = self.store.select_version(self.item['versions'], self.version_id)
        self.next_version = self.store.select_version(self.item['versions'], self.next_version_id)

    def act(self, action, request_id, *, version=None, **fields):
        return self.captures.act(self.item, version or self.version,
                                 {'action': action, 'request_id': request_id, **fields})

    def receipt(self, *, proof=True, missing=True, state='completed', anchor='p1', request_id='turn-1'):
        # Persist a receipt from the actual follow-up writer with real catalog citations.
        cite = next({'id': key, **row} for key, row in make_catalog(
            self.item, self.version, anchor='p1').items() if row['anchor'] == 'p1')
        absent = {'kind': 'limits', 'text': '原文未交代', 'citations': [], 'missing': True}
        evidence = {'kind': 'evidence', 'text': '作者先壓縮文件，再核對候選。模型只處理既有資料。',
                    'citations': [cite], 'missing': False}
        inference = {**evidence, 'kind': 'inference', 'text': '若假設保持不變，才能延用這項結論。'}
        limits = absent if missing else {**evidence, 'kind': 'limits', 'text': '適用於有固定資料集的情境。'}
        sections = [evidence if proof else {**absent, 'kind': 'evidence'}, limits, inference]
        row, _ = self.followups._reserve(
            (self.item_id, int(self.version_id), QUESTION, anchor, 'primary'), request_id, 'local')
        return self.followups._write(row['id'], sections, state, '' if state == 'completed' else 'interrupted')['id']

    def test_original_tags_are_exact_atomic_and_replay_safe(self):
        data = {'anchor': 'p1', 'quote': 'verifies candidates', 'tag': '#可試看看', 'comment': '試驗結果'}
        record = self.act('tag', 'tag-1', **data)
        self.assertEqual(record['kind'], 'tag')
        self.assertEqual(record['state'], 'saved')
        self.assertEqual(record['href'], f'/items/{self.item_id}?version={self.version_id}#p1')
        self.assertEqual(record, self.act('tag', 'tag-1', **data))
        with self.assertRaises(CaptureError) as failure:
            self.act('tag', 'tag-1', **(data | {'comment': '不同輸入'}))
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(len(self.store.get_item(self.item_id)['notes']), 1)
        self.assertIn('試驗結果', self.store.get_item(self.item_id)['notes'][0]['text'])
        self.assertEqual(self.captures.list(self.item_id, self.version_id), [record])
        self.assertEqual(self.captures.list(self.item_id, self.next_version_id), [])
        for fields in (data | {'quote': 'VERIFIES candidates'}, data | {'anchor': 'p2'}, data | {'tag': '#未知'}):
            with self.assertRaises(CaptureError) as invalid:
                self.act('tag', 'invalid', **fields)
            self.assertEqual(invalid.exception.status, 400)
        forged = {**self.version, 'blocks': [{'anchor': 'p1', 'text': 'forged client quote'}]}
        with self.assertRaises(CaptureError):
            self.act('tag', 'forgery', version=forged, **(data | {'quote': 'forged client quote'}))

    def test_failed_capture_rolls_back_note_and_request_receipt(self):
        data = {'anchor': 'p1', 'quote': 'verifies candidates', 'tag': '#想法'}
        with self.store._connect() as conn:
            conn.execute("""CREATE TRIGGER capture_fixture_failure BEFORE INSERT ON capture_records
                            BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.act('tag', 'retry-after-storage-failure', **data)
        self.assertFalse(self.store.get_item(self.item_id)['notes'])
        self.assertFalse(self.captures.list(self.item_id, self.version_id))
        with self.store._connect() as conn:
            conn.execute('DROP TRIGGER capture_fixture_failure')
        saved = self.act('tag', 'retry-after-storage-failure', **data)
        self.assertEqual(saved, self.act('tag', 'retry-after-storage-failure', **data))
        self.assertEqual(len(self.store.get_item(self.item_id)['notes']), 1)

    def test_cached_guide_selection_requires_a_canonical_citation_in_this_version(self):
        guide = cached_guide(self.store, self.item, self.version)
        section = next(s for s in guide['sections'] if any(c['anchor'] == 'p1' for c in s['citations']))
        data = {'anchor': 'p1', 'quote': section['text'], 'tag': '#想法'}
        result = self.act('tag', 'guide-ok', **data)
        self.assertEqual((result['anchor'], result['source'], result['quote']), ('p1', 'guide', section['text']))
        for key, version, attempted in (
            ('fake-anchor', self.version, data | {'anchor': 'p2'}),
            ('cross-edition', self.next_version, data),
            ('made-up', self.version, data | {'quote': '這不是已驗證的導讀內容。'}),
        ):
            with self.subTest(key=key), self.assertRaises(CaptureError):
                self.act('tag', key, version=version, **attempted)

    def test_missing_uses_persisted_question_and_requires_completed_same_version(self):
        turn_id = self.receipt()
        bad = self.receipt(state='failed', request_id='turn-failed')
        no_gap = self.receipt(missing=False, request_id='turn-no-gap')
        result = self.act('missing', 'missing-1', turn_id=str(turn_id))
        self.assertEqual((result['text'], result['tag'], result['anchor']), (QUESTION, '#盲點/待查', 'p1'))
        self.assertEqual(self.store.get_item(self.item_id)['notes'][0]['text'], f'#盲點/待查\n{QUESTION}')
        self.assertEqual(self.act('missing', 'missing-2', turn_id=turn_id)['id'], result['id'])
        with self.assertRaises(CaptureError) as cross:
            self.act('missing', 'cross', version=self.next_version, turn_id=turn_id)
        self.assertEqual(cross.exception.status, 404)
        for failed_id in (bad, no_gap):
            with self.assertRaises(CaptureError):
                self.act('missing', f'ineligible-{failed_id}', turn_id=failed_id)
        self.assertEqual(len(self.store.get_item(self.item_id)['notes']), 1)

    def test_backup_restores_anchors_session_and_mutable_understanding_in_a_clean_directory(self):
        from zipfile import ZipFile
        from knowledge_hub.library import KnowledgeLibrary
        turn = self.receipt()
        tag = self.act('tag','mark-backup',anchor='p1',quote=PARAGRAPH,tag='#想法')
        draft = self.act('draft','draft-backup',turn_id=turn)
        self.act('edit','edit-backup',id=draft['id'],revision=1,text='下次閱讀要再核對適用的設定。')
        session = ReadingState(self.store).save(self.item_id,self.version_id,0,
                         {'anchor':'capture-'+str(tag['id']),'tab':'lesson','question_draft':QUESTION})
        archive = self.db.parent / 'backup.zip'
        KnowledgeLibrary(self.store).backup(archive)
        restored = self.db.parent / 'restored' / 'knowledge.sqlite3'
        restored.parent.mkdir()
        with ZipFile(archive) as saved:
            restored.write_bytes(saved.read('knowledge.sqlite3'))
        store = KnowledgeStore(restored)
        self.assertEqual(ReadingState(store).get(self.item_id,self.version_id),session)
        self.assertEqual(CaptureStore(store,Followups(store)).list(self.item_id,self.version_id),
                         self.captures.list(self.item_id,self.version_id))
        self.assertEqual(Followups(store).get(turn),self.followups.get(turn))

    def test_understanding_reopens_privately_and_uses_compare_and_swap(self):
        turn_id = self.receipt()
        draft = self.act('draft', 'draft-1', turn_id=turn_id)
        self.assertEqual((draft['kind'], draft['state'], draft['revision']), ('understanding', 'draft', 1))
        self.assertIn('作者先壓縮文件，再核對候選。', draft['text'])
        self.assertIn('原文未交代', draft['text'])
        self.assertIn('推論', draft['text'])
        self.assertEqual(draft['text'].count('。'), 3)
        self.assertEqual(self.store.get_item(self.item_id)['notes'], [])
        reopened = CaptureStore(KnowledgeStore(self.db), self.followups)
        self.assertEqual(reopened.list(self.item_id, self.version_id), [draft])

        args = {'id': draft['id'], 'revision': '1', 'text': '自己的理解。還要核對動態索引。'}
        edit = self.act('edit', 'edit-1', **args)
        self.assertEqual((edit['text'], edit['revision']), (args['text'], 2))
        with self.assertRaises(CaptureError) as stale:
            self.act('confirm', 'stale-confirm', turn_id=turn_id, revision=1)
        self.assertEqual(stale.exception.status, 409)
        confirmed = self.act('confirm', 'confirm-2', turn_id=turn_id, revision=2)
        self.assertEqual((confirmed['state'], confirmed['revision']), ('saved', 3))
        self.assertEqual(self.act('confirm', 'confirm-2', turn_id=turn_id, revision=2), confirmed)
        self.assertEqual(self.act('edit', 'edit-1', **args), edit)
        self.assertEqual(self.store.get_item(self.item_id)['notes'][0]['anchor'], 'p1')
        self.assertIn(args['text'], self.store.get_item(self.item_id)['notes'][0]['text'])
        self.assertEqual(reopened.list(self.item_id, self.version_id), [confirmed])
        self.assertEqual(self.act('draft', 'draft-retry', turn_id=turn_id), confirmed)
        bad_turn = self.receipt(proof=False, request_id='no-evidence')
        with self.assertRaises(CaptureError):
            self.act('draft', 'ineligible-draft', turn_id=bad_turn)

    def test_two_concurrent_edits_cannot_overwrite_each_other(self):
        draft = self.act('draft', 'new-draft', turn_id=self.receipt())
        barrier = Barrier(2)

        def edit(number):
            barrier.wait(timeout=3)
            try:
                return self.act('edit', f'tab-{number}', id=draft['id'], revision=1, text=f'分頁 {number}')
            except CaptureError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, (1, 2)))
        self.assertEqual(len([row for row in results if row == 409]), 1)
        saved, = self.captures.list(self.item_id, self.version_id)
        self.assertEqual(saved['revision'], 2)
        self.assertIn(saved, results)

    def test_visual_capture_keeps_exact_figure_and_fixed_publisher_location(self):
        panels = visuals.extract(ARTICLE, URL)
        catalog = make_catalog(self.item, self.version, anchor='S3.T1', panels=panels)
        cite = next({'id': key, **row} for key, row in catalog.items() if row['anchor'] == 'S3.T1')
        absent = {'kind': 'limits', 'text': '原文未交代', 'citations': [], 'missing': True}
        evidence = {'kind': 'evidence', 'text': '圖表清楚列出獨立評測中的結果。',
                    'citations': [cite], 'missing': False}
        row, _ = self.followups._reserve(
            (self.item_id, int(self.version_id), QUESTION, 'S3.T1', 'primary'), 'visual-turn', 'local')
        turn_id = self.followups._write(row['id'], [evidence, absent, {**absent, 'kind': 'inference'}],
                                        'completed')['id']
        self.assertNotIn('S3.T1', {block['anchor'] for block in self.version['blocks']})
        missing = self.act('missing', 'visual-missing', turn_id=turn_id)
        draft = self.act('draft', 'visual-draft', turn_id=turn_id)
        understanding = self.act('confirm', 'visual-understanding', id=draft['id'], revision=1)
        for entry in (missing, understanding):
            self.assertEqual(entry['anchor'], 'S3.T1')
            self.assertEqual(entry['href'], f'/items/{self.item_id}?version={self.version_id}#kh-visual-S3.T1')
            self.assertEqual(entry['source_url'], URL + '#S3.T1')
        self.assertEqual({note['anchor'] for note in self.store.get_item(self.item_id)['notes']},
                         {'kh-visual-S3.T1'})
        self.assertIn(understanding['href'], {hit['href'] for hit in self.captures.search('獨立評測')})

        guide = cached_guide(self.store, self.item, self.version, panels)
        section = next(s for s in guide['sections'] if any(c['anchor'] == 'S3.T1' for c in s['citations']))
        guide_capture = self.act('tag', 'visual-guide', anchor='S3.T1', quote=section['text'], tag='#想法')
        self.assertEqual((guide_capture['source'], guide_capture['href']), ('guide', understanding['href']))

    def test_figure_selected_for_a_fully_missing_turn_uses_the_saved_visual_only(self):
        def unexpected_fetch(url):
            self.fail(f'Capture unexpectedly attempted to fetch {url}')

        editions = ArticleEditions(self.store, unexpected_fetch, lambda _: None)
        manifest = {**visuals.extract(ARTICLE, URL), 'state': 'ready'}
        editions.remember_panels(self.version_id, manifest)
        row, _ = self.followups._reserve(
            (self.item_id, int(self.version_id), QUESTION, 'S3.T1', 'primary'), 'missing-figure-turn', 'local')
        sections = [{'kind': kind, 'text': '原文未交代', 'citations': [], 'missing': True}
                    for kind in ('evidence', 'limits', 'inference')]
        turn_id = self.followups._write(row['id'], sections, 'completed')['id']
        record = self.act('missing', 'selected-figure-gap', turn_id=turn_id)
        self.assertEqual(record['anchor'], 'S3.T1')
        self.assertEqual(record['source_url'], URL + '#S3.T1')
        self.assertTrue(record['href'].endswith('#kh-visual-S3.T1'))

    def test_unfocused_missing_capture_binds_the_edition_without_guessing_a_paragraph(self):
        from knowledge_hub.connections import ConnectionService
        row, _ = self.followups._reserve(
            (self.item_id, int(self.version_id), QUESTION, '', 'primary'), 'unfocused-turn', 'local')
        sections = [{'kind': kind, 'text': '原文未交代', 'citations': [], 'missing': True}
                    for kind in ('evidence', 'limits', 'inference')]
        turn_id = self.followups._write(row['id'], sections, 'completed')['id']
        record = self.act('missing', 'unfocused-gap', turn_id=turn_id)
        self.assertEqual(record['anchor'], '')
        self.assertEqual(record['href'], f'/items/{self.item_id}?version={self.version_id}')
        self.assertEqual(record['text'], QUESTION)
        reopened = ConnectionService(self.store).build_context(self.item_id, self.version_id)
        self.assertEqual(reopened['notes'][0]['source_url'], self.version['source_url'])

    def test_bilingual_recall_covers_legacy_notes_concepts_and_pending_questions(self):
        self.store.add_note(self.item_id, self.version_id, 'p1', '注意 Distributed Inference')
        self.store.add_relation(self.item_id, self.version_id, 'p1', '量化索引', 'requires')
        ReadingState(self.store).save(self.item_id, self.version_id, 0, {'open_questions': '舊的索引重建疑問'})
        missing = self.act('missing', 'save-gap', turn_id=self.receipt())
        by_query = {
            'fixed retrieval': 'title', 'ＤＩＳＴＲＩＢＵＴＥＤ': 'note',
            '量化索引': 'concept', '舊的索引重建': 'question', '長時間斷線': 'question',
        }
        for query, expected in by_query.items():
            with self.subTest(query=query):
                results = self.captures.search(query)
                self.assertTrue(results)
                self.assertIn(expected, {hit['kind'] for hit in results})
                self.assertTrue(all(hit['href'].startswith(f'/items/{self.item_id}?version=') for hit in results))
        self.assertEqual(self.captures.search('長時間斷線')[0]['href'], missing['href'])
        self.assertEqual(self.captures.search('%'), [])
        self.assertEqual(self.captures.search('   '), [])


if __name__ == '__main__':
    unittest.main()
