"""Local questions must keep evidence and lineage across restart without provider calls."""
import tempfile
import unittest
from pathlib import Path
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.sources import parse_arxiv_html
from knowledge_hub.study import StudyError
from knowledge_hub.local_study import LocalStudy


class SourceTurnsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/'test.sqlite3'
        self.store = KnowledgeStore(self.db)
        text = b'<article><h1>Testing retrieval latency</h1><p id="s">Our retrieval pipeline reduces search latency from 80 milliseconds to 26 milliseconds on the ImageGallery benchmark.</p></article>'
        self.id,self.v,_ = self.store.upsert_parsed_entry(parse_arxiv_html(text,'https://arxiv.org/html/2601.01234v1'))
        self.other,self.w,_ = self.store.upsert_parsed_entry(parse_arxiv_html(text.replace(b'ImageGallery',b'IndoorSearch'),'https://arxiv.org/html/2601.09999v1'))
        self.study = LocalStudy(self.store)

    def test_immutable_idempotent_evidence_and_parent(self):
        q='ImageGallery 的檢索延遲是多少？'
        first = self.study.ask(self.id,self.v,'turn1',q)
        self.assertTrue(first['answer']['sections'])
        ref=first['answer']['sections'][0]['citations'][0]
        self.assertEqual((ref['item_id'],str(ref['version_id']),ref['anchor']),(self.id,str(self.v),'s'))
        self.assertEqual(first,self.study.ask(self.id,self.v,'turn1',q))
        follow=self.study.ask(self.id,self.v,'turn2','那限制呢？',first['id'])
        self.assertEqual(follow['parent_id'],first['id'])
        with self.assertRaises(StudyError):
            self.study.ask(self.other,self.w,'wrong','可以跨篇接這一句嗎？',first['id'])
        with self.assertRaises(StudyError):
            self.study.ask(self.id,self.v,'turn1','不同內容')
        self.assertEqual(len(LocalStudy(KnowledgeStore(self.db)).turns(self.id,self.v)),2)
        with self.store._connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM study_records').fetchone()[0],0)
        self.assertEqual(len(self.store.export_data()['source_turns']),2)

    def test_unanswered_question_is_retained_without_making_a_claim(self):
        row=self.study.ask(self.id,self.v,'openq','火星的火山岩如何侵蝕？')
        self.assertEqual(row['answer']['sections'],[])
        self.assertIn('火星',self.study.turns(self.id,self.v)[0]['prompt'])

    def test_carry_an_open_question_to_another_paper_and_reject_a_changed_draft(self):
        from knowledge_hub import study_context
        from knowledge_hub.reading import ReadingState
        reading=ReadingState(self.store)
        saved=reading.save(self.id,self.v,0,{'open_questions':'檢索延遲有哪些證據？'})
        sources=study_context.public_context(self.store,self.other,self.w)['sources']
        earlier=self.study.related_questions(sources)
        self.assertEqual(len(earlier),1)
        answer=self.study.resume(self.other,self.w,'cross-one',earlier[0]['key'])
        self.assertEqual(answer['prompt'],'檢索延遲有哪些證據？')
        self.assertIn(f'/items/{self.id}?version={self.v}',answer['origin'])
        reading.save(self.id,self.v,saved['revision'],{'open_questions':'已修改的私人問題'})
        self.assertEqual(answer,self.study.resume(self.other,self.w,'cross-one',earlier[0]['key']))
        with self.assertRaises(StudyError):
            self.study.resume(self.other,self.w,'cross-later',earlier[0]['key'])

    def test_bookmark_points_to_the_question_in_its_own_edition(self):
        from knowledge_hub.reading import ReadingState
        row=self.study.ask(self.id,self.v,'resume-question','ImageGallery 的檢索延遲？')
        anchor=f'source-turn-{row["id"]}'
        reading=ReadingState(self.store)
        self.assertEqual(reading.save(self.id,self.v,0,{'anchor':anchor})['anchor'],anchor)
        with self.assertRaises(StudyError):
            reading.save(self.other,self.w,0,{'anchor':anchor})

    def test_a_confirmed_understanding_can_be_the_exact_resume_target(self):
        from knowledge_hub.reading import ReadingState
        from knowledge_hub.study import StudyService
        p=StudyService(self.store).save_progress(self.id,self.v,'save-for-bookmark',None,'測試用已確認理解','')
        anchor=f'progress-{p["id"]}'
        reading=ReadingState(self.store)
        self.assertEqual(reading.save(self.id,self.v,0,{'tab':'notes','anchor':anchor})['anchor'],anchor)
        with self.assertRaises(StudyError):
            reading.save(self.other,self.w,0,{'tab':'notes','anchor':anchor})

    def test_related_work_sentences_do_not_restart_section_headings(self):
        from knowledge_hub.local_study import section_labels
        text=['3 Related Work','A survey examined earlier systems.', 'a conclusion of that survey was disputed.',
              '3.1 System design','Other systems use external memory.','4 System Design','We use a separate index.']
        self.assertEqual([r for r,h in section_labels([{'text':t} for t in text])],
                         ['related']*5+['method','method'])
