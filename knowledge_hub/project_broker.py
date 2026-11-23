"""Pinned Quota SDK 2.0 encrypted adapter. No legacy transport or resubmission.

Only public evidence and explicitly submitted questions reach this boundary.
The parent service persists the request key and binding before calling submit.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

from .broker import StudyBrokerProvider, encoded, read_token
from .operation_trace import response as observed


def _unique_json_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate model output field')
        result[key] = value
    return result


class ProjectStudyProvider(StudyBrokerProvider):
    def __init__(self, base_url, token_file, provider, model, *, trust_profile,
                 trust_state, max_input_bytes, max_output_tokens, max_operations,
                 session_id, timeout=30, client_factory=None):
        if base_url != 'https://momonong.me/quota/api/v1':
            raise ValueError('SDK 2.0 接入須使用已核對的正式 HTTPS 入口。')
        if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', v)
               for v in (provider, model)):
            raise ValueError('請指定經 catalog / explain 核對的免費 provider 與 model。')
        if (type(max_input_bytes) is not int or not 4096 <= max_input_bytes <= 32768
                or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192
                or type(max_operations) is not int or not 1 <= max_operations <= 100
                or not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', session_id)
                or type(timeout) not in (int,float) or not 0 < timeout <= 60):
            raise ValueError('請明確設定本次資源上限與固定 session ID。')
        try:
            profile = Path(trust_profile).expanduser()
            profile_bytes = profile.read_bytes()
            if len(profile_bytes) > 32768 or not isinstance(json.loads(profile_bytes), dict):
                raise ValueError()
            state = Path(trust_state).expanduser().absolute()
            for p in (Path(token_file).expanduser().absolute().parent, state.parent):
                info = p.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
                    raise ValueError()
            if state.exists() or state.is_symlink():
                info = state.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                    raise ValueError()
        except (OSError, TypeError, ValueError):
            raise ValueError('請使用可信 profile；token 與持久 trust state 須位於本人擁有的 0700 目錄。') from None
        token = read_token(token_file)
        if client_factory is None:
            try:
                from importlib.metadata import version
                if version('api-quota-broker') != '2.0.0':
                    raise ValueError()
                from quota_broker.client import ProjectClient
                client_factory = ProjectClient
            except (ImportError, ValueError):
                raise ValueError('請先依固定文件安裝 api-quota-broker==2.0.0 與 encrypted 相依。') from None
        try:
            self._client = client_factory(base_url, token, timeout=timeout,
                security_profile='encrypted', trust_profile=profile, trust_state=state)
        except Exception:
            raise ValueError('SDK 2.0 信任或本機設定未通過；不會改用舊版傳輸。') from None
        self.base_url, self.provider, self.model = base_url, provider, model
        self.max_input_bytes, self.max_output_tokens = max_input_bytes, max_output_tokens
        self.max_operations, self.session_id = max_operations, session_id
        self.profile_hash = hashlib.sha256(profile_bytes).hexdigest()
        self._lock = threading.Lock()

    def binding(self):
        return {**super().binding(), 'protocol':'grounded-study-ohttp-v2',
                'sdk_version':'2.0.0', 'execution_policy':'cloud_only',
                'trust_profile_sha256':self.profile_hash}

    def _messages(self, payload):
        data = self._prompt_data(payload)
        # Prior answers help identify the topic; only current excerpts are evidence.
        data['conversation'] = [
            {'prompt': turn['prompt'], 'answer': {
                'sections': [{key: section[key] for key in ('role', 'kind', 'title', 'text')}
                             for section in turn['answer']['sections']],
                'limitations': turn['answer']['limitations'],
            }}
            for turn in data['conversation']
        ]
        return [{'role':'system', 'content':payload['instructions']},
                {'role':'user', 'content':encoded(data)}]

    def task(self, payload):
        return {**super().task(payload), 'execution_policy':'cloud_only'}

    @staticmethod
    def _failure(error, *, submitting=False):
        detail = getattr(error, 'detail', None)
        code = detail.get('error') if isinstance(detail, dict) else None
        # Only authoritative pre-execution rejections prove this attempt failed.
        if submitting and code in {'invalid_request','forbidden','unauthorized','encryption_required','project_limit'}:
            value = {'state':'failed', 'reason_code':'http_rejected'}
        else:
            value = {'state':'unknown', 'reason_code':'transport_unknown'}
        return observed(value,sdk={'error_code':code,'phase':'sdk_submit' if submitting else 'sdk_poll'})

    def _failed(self, value):
        status = value.get('http_status')
        reason = 'provider_http_failed' if type(status) is int and 400 <= status <= 599 else 'provider_failed'
        return self._diagnostic('failed',reason)

    @staticmethod
    def _unknown(value):
        # After result expiry the status retains the cause in gateway_error_code.
        if (value.get('error_code') == 'family_total_deadline'
                or value.get('gateway_error_code') == 'family_total_deadline'):
            return {'state':'unknown','reason_code':'provider_deadline'}
        # Explain only bounded HTTP metadata, never provider error prose. A
        # recognized error is not proof of non-execution or permission to replay.
        status = value.get('http_status')
        reason = 'provider_unknown'
        if type(status) is int:
            if status in (401, 403):
                reason = 'provider_auth_rejected'
            elif status == 503:
                reason = 'provider_unavailable'
            elif 500 <= status <= 599:
                reason = 'provider_server_error'
        return {'state':'unknown', 'reason_code':reason}

    def submit(self, payload):
        if payload.get('provider_binding') != self.binding() or not self._fits(payload):
            raise ValueError('Persisted payload does not match the configured route/bounds')
        try:
            with self._lock:
                value = self._client.submit(self.task(payload))
        except Exception as error:
            return self._failure(error, submitting=True)
        if type(value) is not dict:
            return observed(self._diagnostic('unknown','protocol_unknown'),status=value)
        state = value.get('state')
        if state == 'unknown':
            return observed(self._unknown(value),status=value)
        if state in ('failed','cancelled','rejected','expired'):
            return observed(self._failed(value),status=value)
        if state in ('queued','waiting','running','completed','completed_usage_unknown'):
            return observed({'state':'pending'},status=value)
        return observed(self._diagnostic('unknown','protocol_unknown'),status=value)

    def inspect_unknown(self, operation_id):
        return self.poll(operation_id, inspect_original=True)

    def poll(self, operation_id, *, inspect_original=False):
        if not isinstance(operation_id,str) or not re.fullmatch(r'[A-Za-z0-9._~-]{1,120}',operation_id):
            raise ValueError('Invalid saved operation ID')
        status = None
        outcome = None

        def reply(value):
            return observed(value,status=status,outcome=outcome)

        try:
            with self._lock:
                status = self._client.status(operation_id)
                if type(status) is not dict:
                    return reply(self._diagnostic('unknown','protocol_unknown'))
                state = status.get('state')
                if state == 'unknown':
                    if inspect_original and status.get('result_available') is True:
                        # Only an explicit read of this receipt may inspect the
                        # stored outcome after automatic polling has stopped.
                        try:
                            outcome = self._client.result(operation_id)
                        except Exception:
                            return reply(self._unknown(status))
                        if isinstance(outcome, dict) and outcome.get('state') == 'unknown':
                            reason = self._unknown(outcome)
                            if reason['reason_code'] != 'provider_unknown':
                                return reply(reason)
                    return reply(self._unknown(status))
                if status.get('result_available') is True:
                    outcome = self._client.result(operation_id)
                elif status.get('result_state') in ('pending','storage_failed') or state in ('queued','waiting','running'):
                    return reply({'state':'pending'})
                elif state in ('failed','expired','cancelled'):
                    return reply(self._failed(status))
                else:
                    return reply(self._diagnostic('unknown','protocol_unknown'))
        except Exception as error:
            return reply(self._failure(error))
        if type(outcome) is not dict:
            return reply(self._diagnostic('unknown','protocol_unknown'))
        if outcome.get('state') == 'unknown':
            return reply(self._unknown(outcome))
        if outcome.get('state') not in ('completed','completed_usage_unknown'):
            return reply(self._failed(outcome))
        result = outcome.get('result')
        if type(result) is not dict or not isinstance(result.get('text'), str) or len(result['text'].encode()) > 98304:
            return reply(self._diagnostic('failed','invalid_output'))
        envelopes = (outcome, result)
        if any(e.get('response_truncated') or e.get('truncated') or e.get('finish_reason') in ('length','max_tokens','max_output_tokens') for e in envelopes):
            return reply(self._diagnostic('failed','output_truncated'))
        if (not any(e.get('finish_reason') == 'stop' for e in envelopes)
                or any(e.get('finish_reason') not in (None,'stop') for e in envelopes)):
            return reply(self._diagnostic('failed','incomplete_output'))
        try:
            answer = json.loads(result['text'], object_pairs_hook=_unique_json_fields)
        except (ValueError, TypeError, RecursionError):
            return reply(self._diagnostic('failed','invalid_json'))
        return reply({'state':'completed','answer':answer,
                      'usage_state':'known' if outcome['state']=='completed' else 'unknown'})
