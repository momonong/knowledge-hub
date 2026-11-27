"""An explicit private, same-origin mount for the existing loopback application.

Home validates only password-management authorization. Reading uses KH's own
password and cookie; no Home database, password or verifier is shared.
"""
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie, CookieError
import json
import re
from hmac import compare_digest
from urllib.parse import quote, urlsplit

from .access import ASSETS


HOME_COOKIE = '__Host-momonong-home'


class HomeSessionAuthorizer:
    def __init__(self, port, origin):
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('Invalid Home port')
        self.port = port
        self.host = urlsplit(origin).netloc

    def __call__(self, environ):
        """True=authenticated, False=login required, None=verification unavailable."""
        result = self.session(environ)
        return result['authenticated'] if result is not None else None

    def authorize_change(self, environ):
        result = self.session(environ)
        if result is None:
            return None
        token = environ.get('HTTP_X_CSRF_TOKEN', '')
        csrf = result.get('csrf_token', '')
        return bool(result['authenticated'] and isinstance(csrf, str)
                    and re.fullmatch(r'[A-Za-z0-9_-]{43}', token)
                    and re.fullmatch(r'[A-Za-z0-9_-]{43}', csrf) and compare_digest(token, csrf))

    def session(self, environ):
        raw = environ.get('HTTP_COOKIE', '')
        if len(raw) > 8192 or sum(part.strip().startswith(HOME_COOKIE+'=') for part in raw.split(';')) != 1:
            return {'authenticated': False}
        try:
            cookies = SimpleCookie(raw)
            token = cookies[HOME_COOKIE].value if HOME_COOKIE in cookies else ''
        except CookieError:
            return {'authenticated': False}
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return {'authenticated': False}
        connection = HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request('GET', '/home-api/session', headers={
                'Host': self.host, 'X-Forwarded-Proto': 'https',
                'Cookie': HOME_COOKIE + '=' + token, 'Accept': 'application/json',
            })
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                return None
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get('authenticated')) is not bool:
                return None
            return result
        except (OSError, HTTPException, ValueError):
            return None
        finally:
            connection.close()


def mounted_url(prefix, value):
    return prefix + value if value.startswith('/') and not value.startswith('//') else value


class _MountedHTML(HTMLParser):
    """Rewrite URL attributes, never article text, escaped notes, or script data."""
    def __init__(self, prefix, *, native_reader=False):
        super().__init__(convert_charrefs=False)
        self.prefix, self.parts = prefix, []
        self.native_reader = native_reader

    def _tag(self, *, native_root=False):
        raw = self.get_starttag_text()
        if native_root:
            raw = raw[:-1] + ' data-native-reader>'
        self.parts.append(re.sub(r'\b(href|src|action|formaction)=(\"|\')(/[^\"\']*)\2',
            lambda m: m[1] + '=' + m[2] + mounted_url(self.prefix, m[3]) + m[2], raw))

    def handle_starttag(self, tag, attrs):
        self._tag(native_root=(tag == 'html' and self.native_reader))
        if tag == 'head':
            self.parts.append('<meta name="kh-base-path" content="' + self.prefix + '">')
    def handle_startendtag(self, tag, attrs): self._tag()
    def handle_endtag(self, tag): self.parts.append('</' + tag + '>')
    def handle_data(self, data): self.parts.append(data)
    def handle_entityref(self, name): self.parts.append('&' + name + ';')
    def handle_charref(self, name): self.parts.append('&#' + name + ';')
    def handle_comment(self, data): self.parts.append('<!--' + data + '-->')
    def handle_decl(self, decl): self.parts.append('<!' + decl + '>')


@lru_cache(maxsize=1)
def _fallback_brand():
    """Load the public visual assets for HTML sent without a reader session."""
    styles = '\n'.join((ASSETS / name).read_text() for name in ('palette.css', 'access.css'))
    icon = 'data:image/svg+xml,' + quote((ASSETS / 'book.svg').read_text(), safe='')
    return styles, icon


class MountedApplication:
    def __init__(self, app, *, origin, prefix, authorize, access=None):
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or parsed.netloc != parsed.hostname or origin != 'https://' + parsed.hostname):
            raise ValueError('Hosted origin must be a canonical HTTPS hostname')
        if not re.fullmatch(r'/[a-z][a-z0-9-]*', prefix):
            raise ValueError('Hosted prefix must be one explicit path segment')
        if not callable(authorize):
            raise ValueError('Hosted mode requires session authorization')
        self.app, self.origin, self.host, self.prefix, self.authorize = app, origin, parsed.hostname, prefix, authorize
        self.access = access

    @staticmethod
    def _reply(start, code, body, mime='text/html; charset=utf-8', extra=()):
        payload = body.encode('utf-8')
        headers = [('Content-Type', mime), ('Content-Length', str(len(payload))),
                   ('Cache-Control', 'no-store'), ('Vary', 'Cookie'),
                   ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
                   ('Content-Security-Policy', "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")]
        start(code, headers + list(extra))
        return [payload]

    def __call__(self, environ, start_response):
        if environ.get('HTTP_HOST') != self.host:
            return self._reply(start_response, '400 Bad Request', 'Untrusted Host')
        # Only the explicitly configured, same-host Caddy may assert HTTPS.
        if (environ.get('REMOTE_ADDR') not in ('127.0.0.1', '::1')
                or environ.get('HTTP_X_FORWARDED_PROTO') != 'https'):
            return self._reply(start_response, '403 Forbidden', 'HTTPS proxy required')
        path = environ.get('PATH_INFO', '/')
        if path == self.prefix:
            return self._reply(start_response, '308 Permanent Redirect', '',
                               extra=[('Location', self.prefix + '/')])
        if not path.startswith(self.prefix + '/'):
            return self._reply(start_response, '404 Not Found', 'Not found')
        method = environ.get('REQUEST_METHOD', 'GET').upper()
        if path == self.prefix + '/healthz' and method == 'GET':
            return self._reply(start_response, '200 OK', '{"status":"ok"}', 'application/json')
        if method == 'POST' and environ.get('HTTP_ORIGIN') != self.origin:
            return self._reply(start_response, '403 Forbidden', 'Origin rejected')
        if self.access:
            response = self.access.handle(path, environ, start_response)
            if response is not None:
                return response
        allowed = self.authorize(environ)
        if allowed is not True:
            unavailable = allowed is None
            if self.access:
                return self.access.challenge(start_response, unavailable)
            title = '暫時無法確認登入' if unavailable else '登入後，接著學'
            explanation = ('首頁登入服務暫時無法連線，請稍後再試。你的資料沒有開放給其他人。' if unavailable
                           else 'Knowledge Hub 保存你的閱讀、問題與理解。請先用 Momonong Home 的站主登入，再回來繼續。')
            styles, icon = _fallback_brand()
            body = f'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
              <meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} — Knowledge Hub</title>
              <link rel="icon" sizes="any" type="image/svg+xml" href="{icon}"><style>{styles}</style></head>
              <body><header><a class="brand" href="{self.prefix}/">
              <img class="kh-book" src="{icon}" width="32" height="32" alt="">Knowledge Hub</a></header>
              <main><h1>{title}</h1><p>{explanation}</p>
              <p class="access-actions"><a class="cta" href="/#passwords">前往首頁登入</a>
              <a href="{self.prefix}/">重新確認登入</a></p>
              <p class="hint">使用既有站主帳號，不需要另設一組密碼。</p></main></body></html>'''
            return self._reply(start_response, '503 Service Unavailable' if unavailable else '401 Unauthorized', body)
        inner = dict(environ)
        inner.update(PATH_INFO=path[len(self.prefix):], SCRIPT_NAME=self.prefix,
                     HTTP_HOST='localhost', HTTP_ORIGIN='http://localhost', **{'wsgi.url_scheme':'http'})
        captured = {}
        def capture(status, headers, exc_info=None):
            captured.update(status=status, headers=headers)
        iterator = self.app(inner, capture)
        mime = next((value for key, value in captured.get('headers', ()) if key.lower() == 'content-type'), '')
        if mime.split(';', 1)[0].lower() in ('video/mp4', 'application/pdf', 'text/event-stream'):
            headers = captured['headers'] + [('Vary','Cookie'), ('X-Robots-Tag','noindex, nofollow, noarchive, nosnippet')]
            start_response(captured['status'], headers)
            return iterator
        try:
            payload = b''.join(iterator)
        finally:
            if hasattr(iterator, 'close'): iterator.close()
        headers = captured['headers']
        mime = next((value for key,value in headers if key.lower()=='content-type'), '')
        if mime.startswith('text/html'):
            # A presentation capability only. Authentication has already run above.
            native_reader = 'KnowledgeHubAndroid/2' in environ.get('HTTP_USER_AGENT', '').split()
            parser = _MountedHTML(self.prefix, native_reader=native_reader)
            parser.feed(payload.decode('utf-8')); parser.close()
            document = ''.join(parser.parts)
            if not native_reader:
                document = document.replace('</footer>', '<a href="/">Momonong Home ↗</a></footer>')
            if self.access:
                trust_link = f'<a href="{self.prefix}/access/login?trust=1">信任裝置</a>'
                if native_reader:
                    document = document.replace('<!--kh-device-menu-->', trust_link)
                document = document.replace('</footer>',
                    trust_link + f'<a href="{self.prefix}/access/logout">登出 Knowledge Hub</a></footer>')
            payload = document.encode('utf-8')
        elif mime.startswith('application/json'):
            value = json.loads(payload)
            if isinstance(value,dict) and isinstance(value.get('location'),str):
                value['location'] = mounted_url(self.prefix,value['location'])
                payload = json.dumps(value,ensure_ascii=False).encode('utf-8')
        output_headers = [(key, str(len(payload)) if key.lower()=='content-length' else
                           mounted_url(self.prefix,value) if key.lower()=='location' else value)
                          for key,value in headers]
        output_headers.append(('Vary', 'Cookie, User-Agent' if mime.startswith('text/html') else 'Cookie'))
        output_headers.append(('X-Robots-Tag','noindex, nofollow, noarchive, nosnippet'))
        start_response(captured['status'], output_headers)
        return [payload]
