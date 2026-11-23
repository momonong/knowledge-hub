"""Evidence references prevent model retyping of source mathematics; no model IO."""
import copy
import json
import unittest
from knowledge_hub.broker import StudyBrokerProvider
from knowledge_hub.study import validate_answer

FORMULA = 'Given 𝒢q\\mathcal{G}_{q}, reorganize the activated topology without rewriting memory content.'

def source(sid, quote, anchor='p1'):
    return {'id':sid,'item_id':1 if sid=='S1' else 2,'version_id':'3',
            'content_hash':'public-fixture-'+sid,'title':'Public paper '+sid,'scope':'full',
            'excerpts':[{'anchor':anchor,'quote':quote,'source_url':'https://arxiv.org/abs/2601.00001',
                         'reading_url':'/items/1?version=3#'+anchor}]}

def wire(sources):
    provider=object.__new__(StudyBrokerProvider)
    payload={'instructions':'Public instruction','mode':'qa','prompt':'Public question',
             'sources':sources,'conversation':[],'limitations':'Partial excerpts'}
    return json.loads(provider.prompt(payload).split('\n',1)[1])

def answer(cites):
    return {'sections':[{'role':'answer','kind':'inference','title':'Explanation',
                         'text':'Synthetic test explanation.','citations':cites}],
            'limitations':'定位與逐字引用不代表語義已驗證。'}

class EvidenceReferences(unittest.TestCase):
    def test_model_selects_exact_formula_evidence_without_retyping(self):
        sources=[source('S1',FORMULA)]
        sent=wire(sources)['sources'][0]['excerpts'][0]
        self.assertIn('evidence_id',sent)
        validated=validate_answer(answer([{'evidence_id':sent['evidence_id']}]),sources,'qa')
        cite=validated['sections'][0]['citations'][0]
        self.assertEqual(cite['quote'],FORMULA)
        self.assertEqual((cite['source_id'],cite['anchor'],cite['version_id']),('S1','p1','3'))
        self.assertNotIn('evidence_id',cite,'stored answers retain the established export shape')

    def test_long_excerpts_are_exact_bounded_chunks_and_ids_survive_pruning(self):
        for size in (700,701,1552):
            with self.subTest(size=size):
                original='文' * size
                sources=[source('S1',original),source('S2','A distinct public background.')]
                sent=wire(sources)['sources']
                chunks=sent[0]['excerpts']
                self.assertTrue(all('evidence_id' in e for e in chunks))
                self.assertTrue(all(0<len(e['quote'])<=700 for e in chunks))
                self.assertEqual(''.join(e['quote'] for e in chunks),original)
                self.assertEqual(len({e['evidence_id'] for e in chunks}),len(chunks))
                self.assertEqual(wire([sources[1]])['sources'][0]['excerpts'],sent[1]['excerpts'])
                for e in chunks:
                    value=validate_answer(answer([{'evidence_id':e['evidence_id']}]),sources,'qa')
                    self.assertEqual(value['sections'][0]['citations'][0]['quote'],e['quote'])

    def test_unsent_forged_mixed_and_changed_evidence_are_rejected(self):
        sources=[source('S1',FORMULA),source('S2','A distinct public background.')]
        sent=wire(sources)['sources']
        self.assertIn('evidence_id',sent[0]['excerpts'][0])
        first=sent[0]['excerpts'][0]['evidence_id']; omitted=sent[1]['excerpts'][0]['evidence_id']
        for cite in [{'evidence_id':'unknown'}, {'evidence_id':omitted},
                     {'evidence_id':first,'quote':'model rewrite'}, {'evidence_id':False}]:
            with self.subTest(citation=cite), self.assertRaises(ValueError):
                validate_answer(answer([cite]),sources[:1],'qa')
        changed=copy.deepcopy(sources[:1]);changed[0]['excerpts'][0]['anchor']='different'
        with self.assertRaises(ValueError):
            validate_answer(answer([{'evidence_id':first}]),changed,'qa')

    def test_legacy_quotes_stay_strict_and_new_comparisons_need_both_sources(self):
        sources=[source('S1',FORMULA),source('S2','Memory feedback updates the stored graph.')]
        old={'source_id':'S1','anchor':'p1','quote':FORMULA}
        self.assertEqual(validate_answer(answer([old]),sources,'qa')['sections'][0]['citations'][0]['quote'],FORMULA)
        with self.assertRaises(ValueError):
            validate_answer(answer([{**old,'quote':FORMULA.replace('𝒢q\\mathcal{G}_{q}','Gq')}]),sources,'qa')
        sent=wire(sources)['sources']
        self.assertIn('evidence_id',sent[0]['excerpts'][0])
        cites=[{'evidence_id':s['excerpts'][0]['evidence_id']} for s in sent]
        value=answer(cites);value['sections'][0]['role']='comparison'
        self.assertEqual(len(validate_answer(value,sources,'qa')['sections'][0]['citations']),2)
        value['sections'][0]['citations']=cites[1:]
        with self.assertRaises(ValueError):validate_answer(value,sources,'qa')

if __name__=='__main__':unittest.main()
