"""Loopback-only console server with same-origin and CSRF checks."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import socket


def serve(port, html, state, actions, ready):
    token = secrets.token_hex(24)
    nonce = secrets.token_hex(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def host_ok(self):
            return self.headers.get('Host') in ('127.0.0.1:' + str(port), 'localhost:' + str(port))

        def respond(self, value, status=200, page=False):
            raw = value.encode('utf-8') if page else json.dumps(value, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if page else 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'nonce-" + nonce + "'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.host_ok():
                return self.respond({'error': 'invalid_host'}, 403)
            if self.path == '/':
                return self.respond(html.replace('TOKEN', token).replace('NONCE', nonce), page=True)
            if self.path == '/api/state':
                value = state()
                value['server_pid'] = os.getpid()
                return self.respond(value)
            return self.respond({'error': 'not_found'}, 404)

        def do_POST(self):
            if not self.host_ok() or self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                return self.respond({'error': 'invalid_origin'}, 403)
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), token):
                return self.respond({'error': 'invalid_csrf'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < 4096:
                    raise ValueError('invalid_body_size')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('invalid_body')
                action = actions.get(self.path.removeprefix('/api/')) if self.path.startswith('/api/') else None
                if action is None:
                    return self.respond({'error': 'not_found'}, 404)
                result = action(data)
                self.respond({'ok': True, 'result': result})
            except (ValueError, RuntimeError, KeyError, OSError) as exc:
                self.respond({'error': str(exc)}, 409)

    class ExclusiveServer(ThreadingHTTPServer):
        allow_reuse_address = False
        def server_bind(self):
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
    server = ExclusiveServer(('127.0.0.1', port), Handler)
    ready()
    try:
        server.serve_forever()
    finally:
        server.server_close()
