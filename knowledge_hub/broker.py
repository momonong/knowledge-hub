"""Opt-in, bounded stdlib transport for the KH Broker jobs API.

No retry, redirect, proxy, provider fallback, or credential-bearing diagnostics.
The application calls prepare before reserving the durable operation; submit
never silently changes the persisted evidence.
"""
from __future__ import annotations

import copy
import http.client
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote, urlsplit


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def read_token(path):
    """Read a bounded private regular file using a no-follow descriptor."""
    fd = None
    try:
        from .library import directory
        path = Path(path).expanduser()
        with directory(path.absolute().parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid() or not 1 <= info.st_size <= 8192):
            raise ValueError('Invalid private file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(8193)
        token = raw.decode('ascii').strip()
        if not token or len(raw) > 8192 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise ValueError('Invalid token')
        return token
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Broker token 必須是本人擁有、無群組或其他人權限的非符號連結一般檔案。') from None
    finally:
        if fd is not None:
            os.close(fd)


class BrokerProvider:
    OUTPUT_CEILING = 512
    REQUIRE_FINISH_REASON = False
    def __init__(self, base_url, token_file, provider, model, max_output_tokens=512,
                 *, timeout=15, max_response_bytes=131072):
        try:
            parsed = urlsplit(base_url)
            if (not re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', base_url)
                    or not 1 <= parsed.port <= 65535):
                raise ValueError('Invalid destination')
        except (TypeError, ValueError):
            raise ValueError('Broker URL 必須是 http://127.0.0.1:<port>，不可包含路徑。') from None
        if (not isinstance(provider, str) or not isinstance(model, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', provider)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', model)):
            raise ValueError('Broker provider 與 model 必須明確指定。')
        if (type(max_output_tokens) is not int or not 1 <= max_output_tokens <= self.OUTPUT_CEILING
                or not 0 < timeout <= 60 or not 1024 <= max_response_bytes <= 1048576):
            raise ValueError('Broker 輸出、回應大小或逾時上限無效。')
        self.port = parsed.port
        self.base_url = base_url
        self.provider, self.model = provider, model
        self.max_output_tokens = max_output_tokens
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self._token = read_token(token_file)

    def binding(self):
        """Public connection identity, deliberately excluding token and token hash."""
        return {'base_url': self.base_url, 'provider': self.provider, 'model': self.model,
                'max_output_tokens': self.max_output_tokens}

    def prompt(self, payload):
        # Only the snapshotted public evidence participates in the prompt.
        return payload['instructions'] + '\n' + encoded({k: payload[k] for k in
            ('focus', 'sources', 'limitations', 'context_truncated')})

    def _messages(self, payload):
        return [{'role': 'user', 'content': self.prompt(payload)}]

    def task(self, payload):
        return {'request_key': payload['operation_id'], 'capability': 'text_generation',
                'provider': self.provider, 'model': self.model,
                'input': {'messages': self._messages(payload)},
                'max_output_tokens': self.max_output_tokens, 'max_attempts': 1}

    def _fits(self, payload):
        task = self.task(payload)
        return len(encoded({'input': task['input'], 'options': {}}).encode('utf-8')) <= 4096

    def prepare(self, payload):
        prepared = copy.deepcopy(payload)
        prepared['provider_binding'] = self.binding()
        # The service persists this exact selected evidence and disclosures.
        prepared['context_truncated'] = True
        prepared['limitations'] = '僅傳兩來源有限片段，已依輸入上限裁切；不代表全文。'
        if not self._fits(prepared):
            for source in prepared['sources']:
                source['excerpts'] = source['excerpts'][:1]
            while not self._fits(prepared):
                longest = max((b for s in prepared['sources'] for b in s['excerpts']), key=lambda b: len(b['quote']))
                if len(longest['quote']) <= 40:
                    raise ValueError('Public metadata exceeds Broker input bound')
                longest['quote'] = longest['quote'][:max(40, len(longest['quote']) - 40)]
        return prepared

    def _http(self, method, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=self.timeout)
        try:
            raw = encoded(body).encode('utf-8') if body is not None else None
            connection.request(method, path, body=raw, headers={
                'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json',
                'Content-Type': 'application/json'})
            response = connection.getresponse()
            data = response.read(self.max_response_bytes + 1)
            if len(data) > self.max_response_bytes:
                raise ValueError('Bounded response exceeded')
            status = response.status
            if 300 <= status < 400:
                return status, None
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError, RecursionError):
                value = None
            return status, value
        except Exception:
            # Never propagate HTTP internals, response text, or authorization.
            raise OSError('Broker 傳輸結果不確定；請核對原操作。') from None
        finally:
            connection.close()

    @staticmethod
    def _job(value):
        if type(value) is not dict:
            return {}
        return value.get('job', value) if type(value.get('job', value)) is dict else {}

    @staticmethod
    def _state(job):
        return job.get('state', job.get('status'))

    @staticmethod
    def _diagnostic(state, reason_code, status=None):
        result = {'state': state, 'reason_code': reason_code}
        if status is not None:
            result['http_status'] = status
        return result

    def submit(self, payload):
        if payload.get('provider_binding') != self.binding():
            raise ValueError('Broker connection binding mismatch')
        if not self._fits(payload):
            raise ValueError('Broker input exceeds bound; prepare before persistence')
        try:
            status, value = self._http('POST', '/quota/api/v1/jobs', self.task(payload))
        except OSError:
            return self._diagnostic('unknown', 'transport_unknown')
        if status in (200, 201, 202):
            if type(value) is not dict:
                return self._diagnostic('unknown', 'invalid_json', status)
            job = self._job(value)
            if self._state(job) in ('failed', 'cancelled', 'rejected'):
                return self._diagnostic('failed', 'provider_failed', status)
            # Even an immediately finished job is reconciled by read-only GET.
            return {'state': 'pending'}
        if status in (400, 401, 403, 413, 422, 429):
            return self._diagnostic('failed', 'http_rejected', status)
        return self._diagnostic('unknown', 'http_unknown', status)

    def poll(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id):
            raise ValueError('Invalid operation ID')
        path = '/quota/api/v1/jobs/' + quote(operation_id, safe='')
        try:
            status, value = self._http('GET', path)
        except OSError:
            return self._diagnostic('unknown', 'transport_unknown')
        if status != 200:
            return self._diagnostic('unknown', 'http_unknown', status)
        if type(value) is not dict:
            return self._diagnostic('unknown', 'invalid_json', status)
        job = self._job(value)
        state = self._state(job)
        if state == 'unknown':
            return self._diagnostic('unknown', 'provider_unknown', status)
        if state in ('failed', 'cancelled', 'rejected', 'expired'):
            return self._diagnostic('failed', 'provider_failed', status)
        if state in ('pending', 'queued', 'waiting', 'running', 'submitted'):
            return {'state': 'pending'}
        if state not in ('completed', 'completed_usage_unknown', 'succeeded', 'usage_unknown'):
            return self._diagnostic('unknown', 'protocol_unknown', status)
        try:
            status, value = self._http('GET', path + '/result')
        except OSError:
            return self._diagnostic('unknown', 'transport_unknown')
        if status != 200:
            return self._diagnostic('unknown', 'http_unknown', status)
        if type(value) is not dict:
            return self._diagnostic('failed', 'invalid_json', status)
        outcome_state = self._state(value)
        if outcome_state in ('failed', 'cancelled', 'rejected', 'expired'):
            return self._diagnostic('failed', 'provider_failed', status)
        result = value.get('result')
        if type(result) is not dict or not isinstance(result.get('text'), str):
            return self._diagnostic('failed', 'invalid_output', status)
        for envelope in (value, result):
            if (envelope.get('response_truncated') or envelope.get('truncated')
                    or envelope.get('finish_reason') in ('length', 'max_tokens', 'max_output_tokens')):
                return self._diagnostic('failed', 'output_truncated', status)
            if envelope.get('finish_reason') not in (None, 'stop'):
                return self._diagnostic('failed', 'incomplete_output', status)
        if self.REQUIRE_FINISH_REASON and not any(e.get('finish_reason') == 'stop' for e in (value, result)):
            return self._diagnostic('failed', 'incomplete_output', status)
        if outcome_state not in (None, 'completed', 'completed_usage_unknown', 'succeeded', 'usage_unknown'):
            return self._diagnostic('unknown', 'protocol_unknown', status)
        try:
            answer = json.loads(result['text'])
        except (ValueError, TypeError, RecursionError):
            return self._diagnostic('failed', 'invalid_json', status)
        # Queue completion proves content availability only. Accounting evidence
        # must come from the stored generation outcome, never queue status.
        usage_state = 'known' if outcome_state in ('completed', 'succeeded') else 'unknown'
        if (outcome_state in ('usage_unknown', 'completed_usage_unknown')
                or self._state(result) in ('usage_unknown', 'completed_usage_unknown')
                or value.get('usage_state') == 'unknown' or result.get('usage_state') == 'unknown'):
            usage_state = 'unknown'
        return {'state': 'completed', 'answer': answer,
                'usage_state': usage_state}


class StudyBrokerProvider(BrokerProvider):
    """Explicitly bounded lesson route; old comparison limits stay unchanged.

    Configuration enables capability, not permission to change Broker policy.
    prepare rejects insufficient evidence budgets rather than silently shrinking
    a full lesson into a tiny comparison.
    """
    OUTPUT_CEILING = 8192
    REQUIRE_FINISH_REASON = True

    def __init__(self, base_url, token_file, provider, model, *, max_output_tokens,
                 max_input_bytes, max_operations, session_id, **kwargs):
        if (type(max_input_bytes) is not int or not 4096 <= max_input_bytes <= 65536
                or type(max_operations) is not int or not 1 <= max_operations <= 100
                or not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', session_id)):
            raise ValueError('請明確設定本次 input bytes、操作次數與固定 session ID。')
        self.max_input_bytes, self.max_operations = max_input_bytes, max_operations
        self.session_id = session_id
        super().__init__(base_url,token_file,provider,model,max_output_tokens,**kwargs)

    def binding(self):
        return {**super().binding(), 'max_input_bytes': self.max_input_bytes,
                'max_operations': self.max_operations, 'session_id': self.session_id,
                'protocol': 'grounded-study-v1'}

    def prompt(self, payload):
        return payload['instructions'] + '\n' + encoded(self._prompt_data(payload))

    @staticmethod
    def _prompt_data(payload):
        # URLs and retrieval UI metadata stay in the persisted evidence, not the prompt.
        from .study_context import evidence_spans
        evidence = evidence_spans(payload['sources'])
        sources = [{'id': s['id'], 'title': s['title'], 'scope': s['scope'],
                    'excerpts': [{'evidence_id': key, 'anchor': e['anchor'], 'quote': e['quote']}
                                 for key, e in evidence.items() if e['source_id'] == s['id']]}
                   for s in payload['sources']]
        return {'mode':payload['mode'], 'prompt':payload['prompt'], 'sources':sources,
                'conversation':payload['conversation'], 'context_truncated':True,
                'limitations':payload['limitations']}

    def _fits(self, payload):
        task = self.task(payload)
        return len(encoded({'input':task['input'],'options':{}}).encode()) <= self.max_input_bytes

    def prepare(self, payload):
        from .study import history_variants
        prepared = copy.deepcopy(payload)
        if prepared['provider_binding'] != self.binding():
            raise ValueError('Binding mismatch')
        # Preserve complete source spans, retaining 6 focal / 2 per background.
        # The exact UTF-8 input/options envelope also includes public history.
        while not self._fits(prepared):
            removable = [(len(s['excerpts']) - (6 if s['id'] == 'S1' else 2), s) for s in prepared['sources']]
            removable = [(n,s) for n,s in removable if n > 0]
            if not removable:
                break
            _, source = max(removable,key=lambda pair:pair[0])
            source['excerpts'].pop()
        for history, limits in history_variants(payload['conversation'], payload['limitations']):
            prepared['conversation'] = copy.deepcopy(history)
            prepared['limitations'] = limits
            if self._fits(prepared):
                return prepared
        raise ValueError('Input budget cannot carry the current question, evidence and recent context')
