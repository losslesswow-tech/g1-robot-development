"""Loopback G1 geometry preview and explicitly operated DreamControl console."""
import argparse
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from dreamcontrol_backend import ActionService, Rejected, ROOT


def make_server(service, port=8770):
    sessions = {}
    session_lock = threading.Lock()
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / 'web'), **kwargs)

        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            super().end_headers()

        def guess_type(self, path):
            return 'text/javascript' if str(path).endswith('.mjs') else super().guess_type(path)

        def valid_host(self):
            return self.headers.get('Host') in self.server.allowed_hosts

        def respond(self, value, status=200, cookie=None):
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(raw)

        def session(self, create=False):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                return None, None, None
            item = cookie.get('g1_action_session')
            sid = item.value if item else None
            with session_lock:
                if sid in sessions:
                    return sid, sessions[sid], None
                if not create:
                    return None, None, None
                if len(sessions) >= 128:
                    raise Rejected('session_limit_reached')
                sid, token = secrets.token_hex(24), secrets.token_hex(24)
                sessions[sid] = token
            return sid, token, 'g1_action_session=' + sid + '; HttpOnly; SameSite=Strict; Path=/'

        def do_GET(self):
            if not self.valid_host():
                self.respond(dict(error='invalid_host'), 403)
                return
            parsed = urlsplit(self.path)
            if parsed.path == '/api/dream/config':
                try:
                    sid, csrf, cookie = self.session(True)
                    self.respond(dict(csrf=csrf, **service.config()), cookie=cookie)
                except Rejected as exc:
                    self.respond(dict(error=str(exc)), 409)
            elif parsed.path == '/api/dream/state':
                sid, unused, cookie = self.session()
                page = parse_qs(parsed.query).get('page_id', [''])[0]
                self.respond(service.state((sid, page) if sid else None))
            elif parsed.path.startswith('/api/'):
                self.respond(dict(error='not_found'), 404)
            else:
                super().do_GET()

        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                self.respond(dict(error='origin_rejected'), 403)
                return
            sid, token, cookie = self.session()
            if not sid:
                self.respond(dict(error='session_required'), 403)
                return
            if self.headers.get_content_type() != 'application/json':
                self.respond(dict(error='json_required'), 415)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise ValueError('body_size')
                data = json.loads(self.rfile.read(length), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(data, dict):
                    raise ValueError('object_required')
            except (ValueError, UnicodeError):
                self.respond(dict(error='invalid_json'), 400)
                return
            if not isinstance(data.get('csrf'), str) or not secrets.compare_digest(data['csrf'], token):
                self.respond(dict(error='csrf_rejected'), 403)
                return
            page = data.get('page_id')
            if not isinstance(page, str) or len(page) != 32 or any(c not in '0123456789abcdef' for c in page):
                self.respond(dict(error='page_id_required'), 400)
                return
            path = urlsplit(self.path).path
            if not path.startswith('/api/dream/'):
                self.respond(dict(error='not_found'), 404)
                return
            try:
                self.respond(service.action((sid, page), path[len('/api/dream/'):], data), 202)
            except (Rejected, ValueError) as exc:
                service.audit('request_rejected', action=path, error=str(exc))
                self.respond(dict(error=str(exc)), 409)
            except (OSError, TimeoutError) as exc:
                service.audit('transport_error', error=type(exc).__name__)
                self.respond(dict(error='backend_transport_failure'), 503)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    actual = server.server_address[1]
    server.allowed_hosts = {'127.0.0.1:' + str(actual), 'localhost:' + str(actual)}
    return server


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8770)
    p.add_argument('--backend', choices=('mock', 'ssh'), default='mock')
    p.add_argument('--runtime', type=Path, default=ROOT / 'dreamcontrol-runtime.json')
    p.add_argument('--ssh-key', type=Path)
    p.add_argument('--allow-control', action='store_true')
    p.add_argument('--audit', type=Path, default=ROOT.parent / 'artifacts/dreamcontrol-console-20261015/page-audit.jsonl')
    args = p.parse_args()
    runtime = None
    if args.backend == 'ssh':
        if not args.ssh_key or not args.ssh_key.is_file():
            p.error('existing --ssh-key required')
        runtime = json.loads(args.runtime.read_text(encoding='utf-8-sig'))
    service = ActionService(args.backend, runtime, args.ssh_key, args.allow_control, args.audit)
    server = make_server(service, args.port)
    print(json.dumps(dict(url='http://127.0.0.1:' + str(server.server_address[1]),
                          backend=args.backend, control_enabled=service.config()['control_enabled'],
                          startup_motion_commands=0)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.driver.close()
        server.server_close()


if __name__ == '__main__':
    main()
