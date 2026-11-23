"""Official wire shapes -> actual TLS/OHTTP SDK -> Broker -> KH, provider IO faked.

Run explicitly with the pinned Broker source/tests on PYTHONPATH. No production
credentials, URLs or ledgers are used. References checked on 2026-11-16:
https://aistudio.google.com/api-reference/generate-content/generate-content
https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b-infer
"""
from dataclasses import replace
import json
from pathlib import Path

import pytest
from test_encrypted_api import encrypted_fixture, client_for
import test_gateway_access
from test_gateway_access import TOKENS
from test_study import entry

from quota_broker.family_adapters import PACKAGED_FAMILY_ADAPTERS
from quota_broker.registry import Registry
from knowledge_hub.project_broker import ProjectStudyProvider
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.study import INSTRUCTIONS, StudyService


@pytest.mark.parametrize('provider,model,adapter',[
    ('google','gemini-3.5-flash-lite','gemini_inference'),
    ('nvidia','nvidia/nemotron-3.5-lightning-30b-a3b','openai_inference'),
])
def test_official_minimal_wire_and_completion_at_both_output_bounds(provider,model,adapter):
    registry=Registry.builtin()
    spec=replace(registry.resolve(model),max_output_tokens=8192)
    profile=PACKAGED_FAMILY_ADAPTERS[adapter]
    for maximum in (32,8192):
        task={'capability':'text_generation','input':{'messages':[{'role':'user','content':'Reply OK.'}]},
              'options':{},'max_output_tokens':maximum}
        request=profile.request_task(spec,'fixture-account','fixture-not-a-real-key',task)
        body=json.loads(request.body)
        if provider=='google':
            assert request.url=='https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent'
            assert body=={'contents':[{'role':'user','parts':[{'text':'Reply OK.'}]}],
                          'generationConfig':{'maxOutputTokens':maximum}}
            raw={'candidates':[{'content':{'parts':[{'text':'OK'}]},'finishReason':'STOP'}],
                 'usageMetadata':{'promptTokenCount':4,'candidatesTokenCount':1,'totalTokenCount':5}}
        else:
            assert request.url=='https://integrate.api.nvidia.com/v1/chat/completions'
            assert body=={'model':model,'messages':[{'role':'user','content':'Reply OK.'}],
                          'max_tokens':maximum,'stream':False,'chat_template_kwargs':{'enable_thinking':False}}
            raw={'choices':[{'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':4,'completion_tokens':1,'total_tokens':5}}
        result=profile.interpret_task(spec,'text_generation',200,{'content-type':'application/json'},json.dumps(raw).encode())
        assert result.content['text']=='OK' and result.finish_reason=='stop'


def test_encrypted_minimal_then_kh_lesson_followup_save_restart(tmp_path,monkeypatch):
    tmp_path.chmod(0o700)
    original_target=test_gateway_access.target

    def configured_target(*args,**kwargs):
        target=original_target(*args,**kwargs)
        return replace(target,model='gemini-3.5-flash-lite',max_output_tokens=8192,
                       quotas=tuple(q for q in target.quotas if q.metric=='requests'))

    monkeypatch.setattr(test_gateway_access,'target',configured_target)
    with encrypted_fixture(tmp_path) as fixture:
        _,_,_,gateway,queue,_,policy,_=fixture
        policy_data=json.loads(policy.read_text())
        for principal in policy_data['principals']:
            principal['limits'].update(max_input_bytes=32768,max_output_tokens=8192)
        policy.write_text(json.dumps(policy_data))
        wires=[]

        def provider_boundary(request,timeout,bound):
            body=json.loads(request.body)
            assert request.url.endswith('/gemini-3.5-flash-lite:generateContent')
            assert body['generationConfig']['maxOutputTokens'] in (32,8192)
            text=body['contents'][-1]['parts'][0]['text']
            wires.append(body)
            answer='OK'
            if text!='Reply OK.':
                assert body['systemInstruction']=={'parts':[{'text':INSTRUCTIONS}]}
                payload=json.loads(text)
                cite={'evidence_id':payload['sources'][0]['excerpts'][0]['evidence_id']}
                roles=[('answer','inference')] if payload['mode']=='qa' else [
                    ('problem','author_claim'),('mechanism','author_claim'),('example','example'),
                    ('comparison','inference'),('check','inference')]
                answer=json.dumps({'sections':[{'role':role,'kind':kind,'title':role,
                    'text':'Synthetic protocol fixture; not a model-quality acceptance.',
                    'citations':[cite]} for role,kind in roles],
                    'limitations':'Synthetic provider response for offline wiring verification.'})
            raw={'candidates':[{'content':{'parts':[{'text':answer}]},'finishReason':'STOP'}],
                 'usageMetadata':{'promptTokenCount':4,'candidatesTokenCount':1,'totalTokenCount':5}}
            return 200,{'content-type':'application/json'},json.dumps(raw).encode()

        gateway.family_transport=provider_boundary
        client=client_for(tmp_path,fixture)
        minimal={'request_key':'offline-minimal','capability':'text_generation','provider':'google',
                 'model':'gemini-3.5-flash-lite','input':{'messages':[{'role':'user','content':'Reply OK.'}]},
                 'max_output_tokens':32,'max_attempts':1,'execution_policy':'cloud_only'}
        assert client.submit(minimal)['state']=='queued'
        queue.tick('fixture-worker')
        assert client.result('offline-minimal')['result']['text']=='OK'
        token=tmp_path/'project.token';token.write_text(TOKENS['a']);token.chmod(0o600)
        provider=ProjectStudyProvider('https://momonong.me/quota/api/v1',token,
            'google','gemini-3.5-flash-lite',trust_profile=tmp_path/'profile.json',
            trust_state=tmp_path/'kh-trust.sqlite',max_input_bytes=32768,max_output_tokens=8192,
            max_operations=3,session_id='offline-only',client_factory=lambda *a,**kw:client)
        db=tmp_path/'kh.sqlite3';store=KnowledgeStore(db)
        item,version,_=store.upsert_parsed_entry(entry('arxiv:2601.00001','Memory retrieval',
            'A memory system retrieves relevant observations before generation.'))
        store.add_note(item,version,'p1','PRIVATE_NOTE_SENTINEL')
        service=StudyService(store,provider)
        lesson=service.generate(item,version,'offline-lesson','lesson','Explain the method.')
        assert lesson['state']=='pending'
        queue.tick('fixture-worker')
        lesson=service.reconcile(lesson['id'])
        assert lesson['state']=='completed'
        service.save_progress(item,version,'offline-save',lesson['id'],'PRIVATE_UNDERSTANDING','PRIVATE_QUESTION')
        restarted=StudyService(KnowledgeStore(db),provider)
        assert restarted.progress(item,version)[0]['understanding']=='PRIVATE_UNDERSTANDING'
        question=restarted.generate(item,version,'offline-question','qa','When does this method help?',lesson['id'])
        queue.tick('fixture-worker')
        question=restarted.reconcile(question['id'])
        assert question['state']=='completed' and question['parent_id']==lesson['id']
        restarted.save_progress(item,version,'offline-save-question',question['id'],
                                'PRIVATE_QA_UNDERSTANDING','PRIVATE_NEXT_QUESTION')
        continued=StudyService(KnowledgeStore(db),provider)
        assert continued.records(item)[-1]['id']==question['id']
        assert continued.progress(item,version)[0]['record_id']==question['id']
        assert continued.progress(item,version)[0]['understanding']=='PRIVATE_QA_UNDERSTANDING'
        assert restarted.availability()['used']==2
        assert len(wires)==3
        for private in ('PRIVATE_NOTE_SENTINEL','PRIVATE_UNDERSTANDING','PRIVATE_QUESTION',
                        'PRIVATE_QA_UNDERSTANDING','PRIVATE_NEXT_QUESTION'):
            assert private not in json.dumps(wires)
        assert restarted.events(question['id'])[-1]['details']['broker_result']['http_status']==200
