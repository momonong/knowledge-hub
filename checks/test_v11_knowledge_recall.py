"""The daily reading room only links old records supported by local evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from checks.test_study import entry
from knowledge_hub.captures import CaptureStore
from knowledge_hub.connections import reading_url
from knowledge_hub.knowledge_recall import recommend
from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.study import StudyService


class KnowledgeRecallTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.store = KnowledgeStore(Path(workspace.name) / 'test.sqlite3')

    def paper(self, number, title, text, label='v1'):
        data = entry(f'arxiv:2611.{number:05d}', title, text)
        data['source_url'] = data['source_url'].removesuffix('v1') + label
        data['label'] = label
        item_id, version_id, _ = self.store.upsert_parsed_entry(data)
        item = self.store.get_item(item_id)
        return item, self.store.select_version(item['versions'], str(version_id))

    def cards(self, *cards):
        KnowledgeLibrary(self.store)
        with self.store._connect() as conn:
            for sid, title, url, topic, summary, starter in cards:
                source = {'id': sid, 'title': title, 'primary_url': url, 'topics': [topic], 'summary_zh': summary}
                conn.execute('INSERT INTO library_records VALUES (?,?,?)', ('source', sid, json.dumps(source)))
                if starter:
                    conn.execute('INSERT INTO library_records VALUES (?,?,?)',
                                 ('starter', sid, json.dumps({'source_id': sid, 'learning_goal_zh': summary})))

    def test_bilingual_geometry_prioritizes_actual_overlap_and_deduplicates_same_paper(self):
        focal, version = self.paper(100, 'Stereo inference with camera intrinsics',
                                   'Calibrated binocular vision estimates metric depth and cross-view geometry.')
        self.cards(
            ('same-paper', 'Catalogued original with a different title', 'https://arxiv.org/abs/2611.00100v2',
             '相機內參', '使用相機內參進行推理。', True),
            ('stereo-geometry', 'Stereo calibration notes', 'https://example.com/stereo',
             '雙目視覺', '以相機內參與雙目幾何估計深度。', True),
            ('unrelated', 'Large Models and Approaches', 'https://example.com/other',
             '模型', '大型模型與訓練。', False),
        )
        rows = recommend(self.store, focal, version)
        self.assertEqual([row['href'] for row in rows], ['/library/stereo-geometry'])
        self.assertEqual(rows[0]['kind'], 'starter')
        self.assertIn('「相機內參」', rows[0]['reason'])
        self.assertIn('相機內參', rows[0]['excerpt'])
        self.assertNotIn('你已', rows[0]['reason'])

    def test_saved_private_evidence_links_fixed_records_without_revealing_other_items(self):
        focal, version = self.paper(100, 'Low-bit KV cache quantization',
                                   'We analyze sensitivity of KV cache quantization for streaming inference.')
        old, old_version = self.paper(101, 'Tenant admission control',
                                     'A short unrelated methods section.')
        note_id = self.store.add_note(old['id'], old_version['id'], 'p1',
                                      '排程下要另外量測 KV 快取的回收速度。')['id']
        self.paper(101, 'An updated report', 'An updated, unrelated original.', 'v2')

        question, question_version = self.paper(102, 'Latency testing protocol',
                                               '不同樣本使用相同量測方式。')
        capture = CaptureStore(self.store, None).act(question, question_version, {
            'action': 'tag', 'request_id': 'test-quantization-question', 'anchor': 'p1',
            'quote': '不同樣本使用相同量測方式。', 'tag': '#盲點/待查',
            'comment': '訓練後量化會改變 KV 快取的延遲嗎？',
        })
        understood, understood_version = self.paper(103, 'Memory profiling',
                                                   'Here is a separate text about memory usage.')
        progress = StudyService(self.store).save_progress(
            understood['id'], understood_version['id'], 'test-understood', None, 'KV 快取量化會影響記憶體用量。', '')
        self.store.add_note(focal['id'], version['id'], 'p1', '#盲點/待查\nKV 快取量化是什麼？')
        rows = recommend(self.store, focal, version)
        links = {row['kind']: row for row in rows}
        self.assertEqual(set(links), {'note', 'question', 'understanding'})
        self.assertEqual(links['note']['href'], reading_url(old['id'], old_version['id'], f'note-{note_id}'))
        self.assertEqual(links['note']['title'], 'Tenant admission control')
        self.assertEqual(links['question']['href'],
                         reading_url(question['id'], question_version['id'], f'capture-{capture["id"]}'))
        self.assertIn('延遲嗎', links['question']['excerpt'])
        self.assertEqual(links['understanding']['href'],
                         reading_url(understood['id'], understood_version['id'], f'progress-{progress["id"]}'))
        self.assertTrue(all('快取' in row['reason'] or '量化' in row['reason'] for row in rows))
        reopened = KnowledgeStore(self.store.db_path)
        self.assertEqual(recommend(reopened, focal, version), rows)

    def test_recalled_read_items_are_distinct_from_unread_feed_and_bounded(self):
        focal, version = self.paper(100, 'Low-bit KV cache quantization',
                                   'We investigate KV cache quantization for visual reconstruction.')
        for number in range(101, 107):
            item, _ = self.paper(number, f'KV cache memory study {number}', 'Repeated trials use quantization.')
            self.store.set_item_status(item['id'], 'read')
        self.paper(107, 'KV cache quantization for visual reconstruction', 'This newly fetched paper is unread.')
        rows = recommend(self.store, focal, version)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['kind'] == 'read' and '閱讀紀錄' in row['reason'] for row in rows))
        self.assertEqual(len({row['href'] for row in rows}), 4)
        self.assertTrue(all('study' in row['title'] for row in rows))

    def test_geometry_ranks_above_a_broad_model_family_and_keeps_the_evidence_in_view(self):
        focal, version = self.paper(100, 'Binocular Vision-Language Models for 3D Spatial Reasoning',
                                    'Local constraints require cross-view geometry.')
        old, old_version = self.paper(101, 'Reconstructing scenes from multiple video frames',
                            'The setup records video from a fixed baseline and keeps the entire history of the '
                            'reconstruction in cross–view geometric memory before passing the next frame.')
        self.store.set_item_status(old['id'], 'read')
        self.cards(('broad-vlm', 'Related Foundation Work', 'https://example.com/vlm',
                    '視覺語言模型', '以前訓練視覺與語言模型搭配圖文輸入。', False))
        first, second = recommend(self.store, focal, version)
        self.assertEqual(first['kind'], 'read')
        self.assertEqual(first['href'], reading_url(old['id'], old_version['id'], 'p1'))
        self.assertIn('cross–view geometric', first['reason'])
        self.assertIn('cross–view geometric', first['excerpt'])
        self.assertEqual(second['href'], '/library/broad-vlm')

    def test_custom_concept_tag_matches_an_actual_preexisting_private_question(self):
        focal, version = self.paper(100, 'A localized experimental report', '文中定義了可實作的步驟。')
        older, older_version = self.paper(101, 'My comparison experiment', '保留儀器資料以供覆核。')
        self.store.add_relation(focal['id'], version['id'], 'p1', '動態耦合校準', 'concept')
        self.store.add_note(older['id'], older_version['id'], 'p1',
                            '#盲點/待查\n動態耦合校準需要兩次量測嗎？')
        rows = recommend(self.store, focal, version)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'question')
        self.assertIn('需要兩次量測嗎', rows[0]['reason'])

    def test_domain_and_specific_method_recall_explains_shared_method(self):
        focal, version = self.paper(100, 'World models for autonomous driving',
                                    'We train an action-conditioned dynamics model for trajectory prediction.')
        old, old_version = self.paper(101, 'Trajectory prediction for self-driving vehicles',
                                      'We use action-conditioned dynamics to predict future trajectories.')
        self.store.set_item_status(old['id'], 'read')
        note, note_version = self.paper(102, 'Planning in autonomous driving',
                                        'We evaluate planning through simulations.')
        note_id = self.store.add_note(note['id'], note_version['id'], 'p1',
                                      '動作條件式動態模型能否預測未見過的行車場景？')['id']
        question, question_version = self.paper(103, 'Robot planning',
                                                'We study policies for an industrial robot.')
        self.store.add_note(question['id'], question_version['id'], 'p1',
                            '#盲點/待查\n動作條件式動態模型需要預測多遠？')
        broad, _ = self.paper(104, 'Autonomous driving', 'We report a new dataset with many evaluation scenarios.')
        self.store.set_item_status(broad['id'], 'read')
        rows = {row['kind']: row for row in recommend(self.store, focal, version)}
        self.assertEqual(set(rows), {'read', 'note', 'question'})
        self.assertEqual(rows['note']['href'], reading_url(note['id'], note_version['id'], f'note-{note_id}'))
        self.assertIn('自動駕駛', rows['read']['reason'])
        self.assertIn('動作條件式動態模型', rows['question']['reason'])
        self.assertIn('需要預測多遠', rows['question']['excerpt'])

    def test_generic_words_do_not_create_relationships(self):
        focal, version = self.paper(100, 'A novel AI model',
                                   'Our framework shows high performance and uses several methods and a new dataset.')
        self.cards(('generic', 'AI models and training', 'https://example.com/generic', '模型', '大型模型。', False))
        old, old_version = self.paper(101, 'Some models and methods', 'These AI systems rank high on the benchmark.')
        self.store.set_item_status(old['id'], 'read')
        self.store.add_note(old['id'], old_version['id'], 'p1', 'A high-performing model is a good system.')
        self.assertEqual(recommend(self.store, focal, version), [])


if __name__ == '__main__':
    unittest.main()
