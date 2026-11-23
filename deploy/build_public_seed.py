"""Build a fresh public-only seed. Never clone the source SQLite file.

Supply a reviewed allowlist of public version and document digests.
Only those public originals can enter the new store. Personal records, user
metadata and CSRF state are not queried.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from knowledge_hub.library import KnowledgeLibrary
from knowledge_hub.store import KnowledgeStore
from knowledge_hub.teaching import GUIDES

PUBLIC_HOSTS = {'arxiv.org','github.com','docs.python.org','www.staff.city.ac.uk',
                'aclanthology.org','www.microsoft.com','www2.eecs.berkeley.edu','cormack.uwaterloo.ca'}


def public_url(value):
    parsed=urlsplit(value)
    if parsed.scheme!='https' or parsed.hostname not in PUBLIC_HOSTS or parsed.username or parsed.password:
        raise ValueError('Source outside the reviewed public source hosts')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def version_records(original):
    records=[]
    for row in original.execute('''SELECT i.key,i.source_name,t.title,v.label,v.scope,v.source_url,
            v.author,v.published_at,v.content_hash,v.blocks_json
            FROM item_versions v JOIN items i ON i.id=v.item_id
            JOIN version_titles t ON t.version_id=v.id WHERE i.source_name!='library' ORDER BY v.id'''):
        values=dict(row); values['blocks']=json.loads(values.pop('blocks_json')); records.append(values)
    return records


def document_record(original, document):
    row=original.execute("SELECT payload FROM library_records WHERE category='source' AND id=?",(document['source_id'],)).fetchone()
    old=json.loads(row[0]); mapping=json.loads(document['mapping_json'])
    return {'source_id':document['source_id'],'blob_sha256':document['blob_sha256'],
        'title':old['title'],'authors_or_org':str(old.get('authors_or_org','')),'year':str(old.get('year','')),
        'mapping':{key:mapping[key] for key in ('media_type','source_url','version_label','scope','blocks')}}


def build(source, destination, *, allowlist):
    source=Path(source).resolve(); destination=Path(destination)
    if destination.exists(): raise FileExistsError(destination)
    original=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    original.row_factory=sqlite3.Row
    try:
        documents=[dict(r) for r in original.execute('SELECT * FROM library_documents ORDER BY source_id')]
        versions=version_records(original)
        # Freeze every exported value (including title/author and text) as well
        # as raw-original bytes. A familiar hostname is not a data authorization.
        if (sorted(digest(v) for v in versions)!=sorted(allowlist['versions'])
                or sorted(digest(document_record(original,d)) for d in documents)!=sorted(allowlist['documents'])
                or any(d['source_id'] not in GUIDES for d in documents)):
            raise ValueError('Source differs from the reviewed public deployment set')
        destination.mkdir(parents=True,mode=0o700)
        store=KnowledgeStore(destination/'knowledge.sqlite3')
        library=KnowledgeLibrary(store)
        allowed=documents
        cards=[]
        for document in allowed:
            row=original.execute("SELECT payload FROM library_records WHERE category='source' AND id=?",(document['source_id'],)).fetchone()
            old=json.loads(row[0]); mapping=json.loads(document['mapping_json'])
            cards.append({'id':document['source_id'],'title':old['title'],
                'primary_url':public_url(mapping['source_url']),'primary_domain':'research','domains':['research'],
                'authors_or_org':str(old.get('authors_or_org','')),'year':str(old.get('year',''))})
        library.import_seed({'schema_version':1,'domains':[{'id':'research','title':'研究與背景'}],'sources':cards},
                            {'schema_version':1,'concepts':[],'relations':[]},
                            {'schema_version':1,'items':[{'source_id':s['id'],'order':i+1} for i,s in enumerate(cards)]})
        for document in allowed:
            blob_digest=document['blob_sha256']
            if len(blob_digest)!=64 or any(c not in '0123456789abcdef' for c in blob_digest): raise ValueError('Invalid digest')
            raw=(source.parent/'library/objects'/blob_digest[:2]/blob_digest).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=blob_digest: raise ValueError('Public original hash mismatch')
            mapping=json.loads(document['mapping_json'])
            for block in mapping['blocks']: public_url(block.get('source_url',mapping['source_url']))
            library.add_document(document['source_id'],raw,media_type=mapping['media_type'],
                source_url=public_url(mapping['source_url']),version_label=mapping['version_label'],
                scope=mapping['scope'],blocks=mapping['blocks'],
                extraction_note='從已核對公開原文建立的文字摘錄；以原文範圍標示為準。',
                rights_note='公開來源，供站主私人閱讀與研究；原作者權利保留。')
        # These explicit columns exclude reading status, interests, notes and all
        # user-generated records. Library versions were rebuilt with originals.
        for values in versions:
            public_url(values['source_url'])
            for block in values['blocks']: public_url(block.get('source_url',values['source_url']))
            values['fulltext_status']='ready' if values['scope']=='full' else ('partial' if values['scope']=='partial' else 'not_requested')
            store.upsert_parsed_entry(values)
        with store._connect() as conn:
            private_counts={name:conn.execute('SELECT count(*) FROM '+name).fetchone()[0]
                            for name in ('notes','reading_sessions','study_records','study_progress','questions','lessons')}
            if any(private_counts.values()): raise ValueError('Private state in fresh seed')
            count=conn.execute('SELECT count(*) FROM items').fetchone()[0]
        return {'items':count,'public_originals':len(allowed),'private_rows':private_counts}
    finally:
        original.close()


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--allowlist', type=Path, required=True,
                        help='Reviewed JSON with versions and documents SHA-256 lists')
    args = parser.parse_args()
    allowlist = json.loads(args.allowlist.read_text(encoding='utf-8'))
    print(json.dumps(build(args.source, args.destination, allowlist=allowlist), ensure_ascii=False))
