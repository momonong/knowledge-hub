"""Deployment exports public source records, never personal learning state."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from knowledge_hub.store import KnowledgeStore
from knowledge_hub.reading import ReadingState
from knowledge_hub.library import KnowledgeLibrary
from checks.test_study import entry


class PublicSeed(unittest.TestCase):
    def test_new_store_excludes_notes_drafts_private_metadata_and_original_csrf(self):
        spec=importlib.util.spec_from_file_location('seed',Path(__file__).parents[1]/'deploy/build_public_seed.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=KnowledgeStore(root/'original.sqlite3'); KnowledgeLibrary(store)
            item,version,_=store.upsert_parsed_entry(entry('arxiv:2601.00001','Public paper','Public evidence'))
            ReadingState(store).save(item,version,0,{'reflection':'PRIVATE_SENTINEL'})
            with store._connect() as c:
                c.execute("INSERT INTO library_records VALUES ('metadata','private','{\"note\":\"PRIVATE_SENTINEL\"}')")
            original_csrf=store.get_csrf_token()
            # An independently specified deployment manifest for this fixture.
            public=entry('arxiv:2601.00001','Public paper','Public evidence')
            approved={key:public.get(key,'') for key in ('key','source_name','title','label','scope','source_url','author','published_at','content_hash')}
            approved['blocks']=public['blocks']
            pin=hashlib.sha256(json.dumps(approved,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            allowlist={'versions':[pin],'documents':[]}
            result=module.build(store.db_path,root/'site',allowlist=allowlist)
            self.assertEqual(result['items'],1)
            output=KnowledgeStore(root/'site/knowledge.sqlite3')
            self.assertNotIn('PRIVATE_SENTINEL',json.dumps(output.export_data()))
            self.assertNotEqual(original_csrf,output.get_csrf_token())
            self.assertEqual(output.get_item(1)['versions'][0]['blocks'][0]['text'],'Public evidence')
            self.assertEqual(ReadingState(output).get(1,1)['reflection'],'')
            self.assertEqual(ReadingState(store).get(item,version)['reflection'],'PRIVATE_SENTINEL')
            with self.assertRaises(FileExistsError): module.build(store.db_path,root/'site',allowlist=allowlist)
            store.upsert_parsed_entry(entry('arxiv:2601.00002','Unexpected same-host content','PRIVATE_UNREVIEWED'))
            with self.assertRaisesRegex(ValueError,'reviewed'):
                module.build(store.db_path,root/'unreviewed',allowlist=allowlist)


if __name__=='__main__': unittest.main()
