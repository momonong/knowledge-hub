"""Private JSON exports retain one SQLite snapshot across sections."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from checks.test_initial_library import seed
from knowledge_hub.captures import CaptureStore
from knowledge_hub.followup import Followups
from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.reading import ReadingState
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.store import KnowledgeStore


URL = 'https://arxiv.org/html/2611.04567v1'
PARAGRAPH = 'We verify each retrieval candidate against its source document.'


class PhaseOneExportTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'reading.sqlite3'
        self.store = KnowledgeStore(self.db)
        entry = parse_arxiv_html(f'<article><h1>Retrieval</h1><p id="p1">{PARAGRAPH}</p></article>'.encode(),URL)
        self.item_id,self.version_id,_ = self.store.upsert_parsed_entry(entry)

    def test_export_without_library_is_read_only_and_keeps_empty_library_shape(self):
        with self.store._connect() as conn:
            before = conn.execute('PRAGMA schema_version').fetchone()[0]
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='library_records'").fetchone())
        result = self.store.export_data()
        self.assertEqual(result['schema_version'],1)
        self.assertEqual(result['library'],{'schema_version':1,'domains':[],'sources':[],
                                          'concepts':[],'relations':[],'starter':[],'metadata':{},'documents':[]})
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('PRAGMA schema_version').fetchone()[0],before)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='library_records'").fetchone())

    def test_concurrent_capture_and_document_never_mix_with_older_notes_and_items(self):
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        reader_library = KnowledgeLibrary(self.store)
        reader_library.import_seed(*seed())
        writer = KnowledgeStore(self.db)
        followups = Followups(writer)
        captures = CaptureStore(writer,followups)
        writer_library = KnowledgeLibrary(writer)
        item = writer.get_item(self.item_id)
        version = item['versions'][0]
        initial = captures.act(item,version,{'action':'tag','request_id':'capture-before','tag':'#想法',
                                            'anchor':'p1','quote':'retrieval candidate'})
        original_hydrate = self.store._Hydrate_item
        before = self.store.export_data()
        committed = []

        def hydrate_then_commit(conn, row):
            hydrated = original_hydrate(conn,row)  # The export has read this item's notes.
            if row['id'] == self.item_id and not committed:
                captured = captures.act(item,version,{'action':'tag','request_id':'capture-racing',
                                                     'tag':'#可試看看','anchor':'p1','quote':'source document'})
                writer.set_interests('新興趣')
                writer.update_source_state('arxiv','updated')
                ReadingState(writer).save(self.item_id,self.version_id,0,{'question_draft':'競態時送出的草稿'})
                events = list(followups.run(item,version,'作者的方法是什麼？','p1','after-export-read'))
                self.assertEqual(events[-1][0],'done')
                doc = writer_library.add_document('sample-rag',b'Synthetic original for the export race.',
                    media_type='text/plain',source_url='https://example.org/rag',version_label='export-test',
                    blocks=[{'anchor':'p1','text':'Original source reading fixture.','source_url':'https://example.org/rag'}])
                committed.append((captured,doc))
            return hydrated

        with patch.object(self.store,'_Hydrate_item',side_effect=hydrate_then_commit):
            exported = self.store.export_data()
        self.assertEqual(len(committed),1)
        self.assertEqual(exported,before)
        notes = {note['id'] for item in exported['items'] for note in item['notes']}
        self.assertEqual({row['note_id'] for row in exported['captures']},{initial['note_id']})
        self.assertTrue(all(row['note_id'] in notes for row in exported['captures']))

        after = self.store.export_data()
        new_capture,doc = committed[0]
        self.assertIn(new_capture['id'],[row['id'] for row in after['captures']])
        self.assertTrue(any(row['id']==doc['item_id'] for row in after['items']))
        self.assertEqual(after['library'],reader_library.export_metadata())
        self.assertEqual(after['library']['documents'],[doc])
        self.assertEqual(after['interests'],['新興趣'])
        self.assertEqual(after['sources']['arxiv']['state'],'updated')
        self.assertEqual(after['reading_sessions'][0]['question_draft'],'競態時送出的草稿')
        self.assertEqual(after['followups'][0]['state'],'completed')


if __name__ == '__main__':
    unittest.main()
