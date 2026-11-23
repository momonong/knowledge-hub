"""CLI entrypoint for running Knowledge Hub locally on 127.0.0.1."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import Event, Thread
from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler

from .daily import run_hosted_refresh
from .web import create_app

BIND_HOST = "127.0.0.1"


class ReadingServer(ThreadingMixIn, WSGIServer):
    """Public-source fetches must not prevent saving a private reading draft."""
    daemon_threads = True


class PrivateRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # Search queries and URLs can contain personal reading context.
        pass


def _environment_bool(parser, name):
    value = os.environ.get(name, 'false').strip().casefold()
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    parser.error(f'{name} 須設定為 true/false 或 1/0。')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.12 -B -m knowledge_hub",
        description="Knowledge Hub 本機單人閱讀應用 (僅綁定 127.0.0.1)",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="SQLite 資料庫檔案路徑（建議置於程式目錄外的本機路徑）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="本機監聽連接埠 (預設: 8765)",
    )
    parser.add_argument('--broker-base-url', help='啟用雙來源解說的本機 Broker URL')
    parser.add_argument('--broker-token-file', help='伺服器端私有 token 檔路徑；不要在 argv 放 key')
    parser.add_argument('--broker-provider', help='明確指定 provider，無自動 fallback')
    parser.add_argument('--broker-model', help='明確指定 model')
    parser.add_argument('--broker-protocol', choices=('legacy','encrypted'), default='legacy',
                        help='新版接入明確指定 encrypted；legacy 僅保留原 loopback 相容性，兩者不互相 fallback')
    parser.add_argument('--broker-trust-profile', help='經獨立可信管道核對的 SDK 2.0 公開 profile 檔')
    parser.add_argument('--broker-trust-state', help='0700 目錄內持久 trust state 路徑；不可刪除來重試')
    parser.add_argument('--connection-max-output-tokens', type=int, default=512,
                        help='雙來源初步比較輸出 token 上限（1..512，預設 512）')
    parser.add_argument('--enable-study', action='store_true', help='啟用通用教學與追問；須另明確設定下列資源上限')
    parser.add_argument('--study-max-input-bytes', type=int, help='每次輸入包絡上限（4096..65536 bytes）')
    parser.add_argument('--study-max-output-tokens', type=int, help='每次輸出上限（1..8192 tokens）')
    parser.add_argument('--study-max-operations', type=int, help='此 session 最多新操作數（1..100，跨重啟保存）')
    parser.add_argument('--study-session-id', help='本次已授權 session 的固定非秘密識別值；不可換值繞過上限')
    parser.add_argument('--public-origin', help='正式 HTTPS origin；須同時設定子路徑與 Home session port')
    parser.add_argument('--base-path', help='正式服務子路徑，例如 /knowledge')
    parser.add_argument('--home-session-port', type=int, help='同機 Home 的 loopback port，僅核對修改 KH 密碼的管理權限')
    for role in ('primary', 'fallback'):
        for option, label in (('base-url', 'OpenAI-compatible API base URL，含 /v1'), ('model', '模型識別值'),
                              ('token-file', '伺服器端憑證檔案路徑，可省略')):
            env = 'KH_' + (role + '_' + option).replace('-', '_').upper()
            parser.add_argument(f'--{role}-{option}', default=os.environ.get(env), help=f'{label}（{env}）')
        parser.add_argument(f'--{role}-reasoning-effort', choices=('none','low','medium','high'),
                            default=os.environ.get(f'KH_{role.upper()}_REASONING_EFFORT'),
                            help='只有相容模型才設定 reasoning_effort')
        structured_env = f'KH_{role.upper()}_STRUCTURED'
        parser.add_argument(f'--{role}-structured', action=argparse.BooleanOptionalAction,
                            default=_environment_bool(parser, structured_env),
                            help=f'啟用相容端點的嚴格 JSON schema（{structured_env}）')
    parser.add_argument('--brief-model', choices=('primary','fallback','local'), default=os.environ.get('KH_BRIEF_MODEL'),
                        help='每日簡報預設沿用已配置的 primary，可另指定 fallback 或 local')
    parser.add_argument('--refresh-once', action='store_true', help='立即更新指定資料庫並列出今天已快取的精選，然後結束')
    parser.add_argument('--quiet', action='store_true', help='不記錄含搜尋與閱讀資訊的 HTTP access log')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (1 <= args.port <= 65535):
        parser.error("--port 必須介於 1 到 65535 之間")

    hosted_args = (args.public_origin, args.base_path, args.home_session_port)
    if any(value is not None for value in hosted_args) and not all(hosted_args):
        parser.error('正式接入須同時設定 public-origin、base-path 與 home-session-port。')

    db_path = Path(args.db).expanduser().resolve()
    broker_args = (args.broker_base_url, args.broker_token_file, args.broker_provider, args.broker_model)
    connection_provider = None
    study_provider = None
    study_args = (args.study_max_input_bytes,args.study_max_output_tokens,args.study_max_operations,args.study_session_id)
    if args.broker_protocol == 'encrypted':
        if not args.enable_study or not args.broker_trust_profile or not args.broker_trust_state:
            parser.error('新版加密接入須啟用 study 並指定可信 profile 與持久 trust state。')
    elif args.broker_trust_profile or args.broker_trust_state:
        parser.error('信任設定須同時指定 --broker-protocol encrypted。')
    if args.enable_study and (not all(broker_args) or not all(value is not None for value in study_args)):
        parser.error('啟用通用教學須明確設定 Broker 四項、本次 input/output/operations 與 session ID。')
    if not args.enable_study and any(value is not None for value in study_args):
        parser.error('通用教學設定須同時明確指定 --enable-study。')
    if args.enable_study and args.connection_max_output_tokens != 512:
        parser.error('通用教學與舊雙來源輸出設定請分開啟用。')
    if any(value is not None for value in broker_args):
        if not all(broker_args):
            parser.error('啟用 Broker 時必須同時提供 URL、token 檔、provider 與 model。')
        from .broker import BrokerProvider, StudyBrokerProvider
        try:
            if args.enable_study:
                provider_class, extra = StudyBrokerProvider, {}
                if args.broker_protocol == 'encrypted':
                    from .project_broker import ProjectStudyProvider
                    provider_class = ProjectStudyProvider
                    extra = {'trust_profile':args.broker_trust_profile,'trust_state':args.broker_trust_state}
                study_provider = provider_class(*broker_args,
                    max_input_bytes=args.study_max_input_bytes,max_output_tokens=args.study_max_output_tokens,
                    max_operations=args.study_max_operations,session_id=args.study_session_id,**extra)
            else:
                connection_provider = BrokerProvider(*broker_args, max_output_tokens=args.connection_max_output_tokens)
        except ValueError as error:
            parser.error(str(error))
    elif args.connection_max_output_tokens != 512:
        parser.error('設定解說輸出上限時必須一併設定 Broker。')
    from .model_stream import ModelConfig, ModelStreamError
    routes = {}
    for role in ('primary', 'fallback'):
        url, model, token = (getattr(args, f'{role}_{k}') for k in ('base_url','model','token_file'))
        effort = getattr(args, f'{role}_reasoning_effort')
        structured = getattr(args, f'{role}_structured')
        if any((url, model, token, effort, structured)):
            if not url or not model:
                parser.error(f'{role} 路由須同時設定 base-url 與 model。')
            try:
                routes[role] = ModelConfig(url, model, token, effort, structured=structured)
            except ModelStreamError:
                parser.error(f'{role} 模型設定無效。')
    brief_generator = None
    brief_route = args.brief_model or ('primary' if 'primary' in routes else 'local')
    if brief_route != 'local':
        if brief_route not in routes:
            parser.error('每日預算所選的模型路由尚未設定。')
        from .briefs import model_generator
        from .model_stream import stream_model
        brief_generator = model_generator(routes[brief_route], stream_model)
    app = create_app(db_path, connection_provider=connection_provider, study_provider=study_provider,
                     public_projects=True, model_routes=routes, brief_generator=brief_generator)
    if args.refresh_once:
        from .daily import DailyRefresh
        from .store import KnowledgeStore
        status = app.refresh_daily_sources(prefetch=False, force=True)
        cache = DailyRefresh(KnowledgeStore(db_path))
        cards = cache.selection(status['day'])
        public_cards = [{k: card[k] for k in ('item', 'version', 'category', 'spectrum_reason', 'brief')} for card in cards]
        print(json.dumps({'refresh': status, 'cards': public_cards}, ensure_ascii=False, indent=2))
        return 0 if len(cards) == 5 and status['state'] in ('completed','partial') else 1
    stop_refresh = Event()
    refresh_worker = Thread(target=run_hosted_refresh, args=(app.refresh_daily_sources, stop_refresh),
                            daemon=True, name='kh-discovery')
    if args.public_origin:
        from .hosting import HomeSessionAuthorizer, MountedApplication
        from .access import ReadingAccess
        try:
            owner = HomeSessionAuthorizer(args.home_session_port, args.public_origin)
            access = ReadingAccess(db_path.with_name('access.sqlite3'), prefix=args.base_path,
                                   owner=owner.authorize_change)
            app = MountedApplication(app, origin=args.public_origin, prefix=args.base_path,
                                     authorize=access.authorize, access=access)
        except ValueError as error:
            parser.error(str(error))

    options = {'handler_class': PrivateRequestHandler} if args.quiet else {}
    with make_server(BIND_HOST, args.port, app, server_class=ReadingServer, **options) as httpd:
        refresh_worker.start()
        print(
            f"Knowledge Hub listening on http://{BIND_HOST}:{args.port} (db: {db_path})",
            file=sys.stderr,
            flush=True,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop_refresh.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
