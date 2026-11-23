import json
import unittest

from knowledge_hub import briefs


class ModelBriefs(unittest.TestCase):
    def test_only_public_source_content_is_submitted_and_seven_fields_are_cached(self):
        pick = {'item': {'title':'Public title', 'status':'PRIVATE_READING'},
                'category':'多模態', 'spectrum_reason':'跨模態對齊',
                'reasons':[{'title':'PRIVATE_NOTE'}],
                'version': {'label':'v1','scope':'abstract','blocks':[{'anchor':'abstract','text':'Public abstract'}]}}
        result = {'l1':dict(change='改動',highlight='亮點',relevance='與跨模態對齊的關聯'),
                  'l2':dict(mechanism='做法',metrics='原文未交代',comparison='比較',limits='限制')}
        def stream(config, messages):
            self.assertIn('Public abstract', messages[-1]['content'])
            self.assertNotIn('PRIVATE', json.dumps(messages))
            yield json.dumps(result)
        generator = briefs.model_generator(object(), stream)
        self.assertEqual(briefs.build_brief(pick, generator), {**result,'attempts':1,'error':'','state':'ready'})
