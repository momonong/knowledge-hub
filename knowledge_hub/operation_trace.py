"""Local, bounded operation evidence. Never persist arbitrary error text or bodies."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path


ENUMS = {
    'state': {'queued','waiting','running','pending','completed','completed_usage_unknown',
              'unknown','failed','cancelled','expired','rejected'},
    'error_code': {'provider_http_error','execution_uncertain','transport_unknown','family_total_deadline',
                   'invalid_request','forbidden','unauthorized','encryption_required',
                   'project_limit','not_found','conflict','trust_unavailable','provider_error'},
    'usage_source': {'provider_reported','observed_http_request','unknown'},
    'ledger_state': {'reserved','dispatched','completed','failed','unknown','quota_rejected','expired'},
    'ledger_basis': {'settled_provider_usage','observed_http_request','held_estimate','rejected_zero_usage'},
    'finish_reason': {'stop','length','max_tokens','max_output_tokens','content_filter','tool_calls'},
    'phase': {'transport','parse_response','validate_response','serialize_result','sdk_submit','sdk_poll'},
    'exception_type': {'ValueError','TypeError','KeyError','ProviderError','TimeoutError',
                       'OSError','JSONDecodeError','UnicodeDecodeError'},
    'http_category': {'success','client_error','server_error'},
    'body_shape': {'nested_error','other_object','array','empty','non_json'},
    'content_type': {'application/json','text/html','text/plain','text/event-stream'},
}
NUMBERS = {'attempt_count':100,'latency_ms':3600000,'reported_input_tokens':10**9,
           'reported_output_tokens':10**9,'reported_neurons':10**9}
FLAGS = {'response_complete','terminal_error_response','response_truncated','result_available'}


def summary(value):
    """Use fixed enums, bounded numbers and completion flags, never raw IDs/headers."""
    if type(value) is not dict:
        return {}
    result = {}
    sources = [value]
    if type(value.get('diagnostics')) is dict:
        sources.append(value['diagnostics'])
    for source in sources:
        for name, allowed in ENUMS.items():
            item = source.get(name)
            if type(item) is str and item in allowed:
                result.setdefault(name,item)
        for name, maximum in NUMBERS.items():
            item = source.get(name)
            if type(item) is int and 0 <= item <= maximum:
                result.setdefault(name,item)
            elif name in source and item is None and name.startswith('reported_'):
                result.setdefault(name,None)
        for name in FLAGS:
            if type(source.get(name)) is bool:
                result.setdefault(name,source[name])
    status = value.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    resources = value.get('reported_resources')
    if type(resources) is dict:
        result['reported_resources'] = {k:v for k,v in resources.items()
            if k in {'requests','input_tokens','output_tokens','total_tokens','neurons'}
            and type(v) is int and 0 <= v <= 10**9}
    return result


def layers(value):
    if type(value) is not dict:
        return {}
    return {name:summary(value[name]) for name in ('broker_status','broker_result','sdk') if name in value}


def response(value, *, status=None, outcome=None, sdk=None):
    trace = layers(value.get('trace'))
    for name, data in (('broker_status',status),('broker_result',outcome),('sdk',sdk)):
        if data is not None:
            trace[name] = summary(data)
    return {**value,'trace':trace} if trace else value


def fingerprint(value):
    raw = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def implementation_fingerprint():
    root = Path(__file__).parent
    names = ('study.py','study_context.py','broker.py','project_broker.py','operation_trace.py')
    return fingerprint({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names})


def append(conn, record_id, event, at, details):
    assert event in {'prepared','submit','poll'}
    conn.execute('INSERT INTO study_events(record_id,event,created_at,details_json) VALUES (?,?,?,?)',
                 (record_id,event,at,json.dumps(details,ensure_ascii=False,sort_keys=True,allow_nan=False)))


VALIDATION_CODES = {
    'Invalid answer':'answer_shape', 'Missing limitations':'limitations_missing',
    'Invalid sections':'sections_count', 'Invalid section':'section_shape',
    'Invalid classification':'section_classification',
    'Example/comparison classification mismatch':'section_classification',
    'Invalid text':'section_text', 'Missing citation':'citation_missing',
    'Citation outside sent evidence':'citation_not_in_sent_evidence',
    'Invalid citation fields':'citation_fields', 'Invalid citation types':'citation_types',
    'Comparison needs both originals':'comparison_sources', 'Incomplete lesson':'lesson_roles',
}
