#!/usr/bin/env python3
"""Read-only Obsidian search. No shell, write, or model-selected paths."""
import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
import sys
import cerebro_retrieval
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VAULT = Path(os.environ['CEREBRO_VAULT'])
TOKEN_FILE = Path(os.environ.get('CEREBRO_DATA_DIR', str(Path.home() / '.local/share/cerebro'))) / 'token'
HOST, PORT = '127.0.0.1', 18767
MAX_FILES, MAX_BYTES, MAX_RESULTS = 10000, 1048576, 5
MAX_CONTENT = 40000

def normalize(value):
    text = unicodedata.normalize('NFD', value.casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[\s_-]+', ' ', text).strip()

def read_note(root_fd, parts):
    """Resolve every component under the fixed root using no-follow descriptors."""
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Not a regular Markdown file')
            if info.st_size > MAX_BYTES:
                raise ValueError('Markdown exceeds the 1 MiB read limit')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Markdown exceeds the 1 MiB read limit')
            return raw.decode('utf-8-sig', errors='replace')
        finally:
            os.close(fd)
    finally:
        os.close(current)

def list_notes(root_fd):
    paths, errors = [], []
    count = 0
    def visit(fd, prefix):
        nonlocal count
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > MAX_FILES:
                    raise ValueError('Vault traversal exceeds the 10000 entry limit')
                if entry.name.startswith('.') or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    try:
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            visit(child, prefix + (entry.name,))
                        finally:
                            os.close(child)
                    except OSError as exc:
                        errors.append({'path': '/'.join(prefix + (entry.name,)), 'error': str(exc)})
                elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith('.md'):
                    paths.append(prefix + (entry.name,))
    visit(root_fd, ())
    return sorted(paths), errors

def search(body):
    if not isinstance(body, dict) or set(body) != {'query'}:
        return {'success': False, 'results': [], 'error': 'Only the query field is accepted'}
    query = body['query']
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        return {'success': False, 'results': [], 'error': 'query must be a nonempty string of at most 200 characters'}
    if any(c in query for c in ('/', '\\', ':', '..')) or any(ord(c) < 32 for c in query):
        return {'success': False, 'results': [], 'error': 'Paths and control characters are not accepted'}
    query = query.strip()
    needle = normalize(re.sub(r'\.md$', '', query, flags=re.I))
    if not needle:
        return {'success': False, 'results': [], 'error': 'Empty normalized query'}
    root_fd = None
    try:
        root_fd = os.open(VAULT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        paths, errors = list_notes(root_fd)
        matches = []
        for parts in paths:
            name = normalize(parts[-1][:-3])
            rank = 0 if name == needle else 1 if needle in name else 2
            matches.append((rank, parts))
        matches.sort(key=lambda item: (item[0], item[1]))
        results, matched_count = [], 0
        for rank, parts in matches:
            # Filename matches are always returned before content matches.
            try:
                content = read_note(root_fd, parts)
            except (OSError, ValueError) as exc:
                errors.append({'path': '/'.join(parts), 'error': str(exc)})
                continue
            if rank == 2 and needle not in normalize(content):
                continue
            matched_count += 1
            if len(results) < MAX_RESULTS:
                result = {'file': parts[-1], 'path': str(VAULT.joinpath(*parts)), 'content': content[:MAX_CONTENT]}
                if len(content) > MAX_CONTENT:
                    result['truncated'] = True
                    result['total_chars'] = len(content)
                results.append(result)
        response = {'success': not errors, 'results': results}
        if errors:
            response['errors'] = errors[:20]
        if matched_count > MAX_RESULTS:
            response['truncated'] = True
            response['total_matches'] = matched_count
        return response
    except (OSError, ValueError) as exc:
        return {'success': False, 'results': [], 'error': str(exc)}
    finally:
        if root_fd is not None:
            os.close(root_fd)

class Handler(BaseHTTPRequestHandler):
    server_version = 'ObsidianReadOnly/1'
    def reply(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)
    def do_POST(self):
        if self.path not in ('/search', '/retrieve'):
            self.reply(404, {'success': False, 'results': [], 'error': 'Unknown endpoint'})
            return
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            self.reply(401, {'success': False, 'results': [], 'error': 'Unauthorized'})
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise ValueError('application/json required')
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError('Body must be between 1 and 4096 bytes')
            body = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError) as exc:
            self.reply(400, {'success': False, 'results': [], 'error': str(exc)})
            return
        self.reply(200, search(body) if self.path == '/search' else cerebro_retrieval.search(sys.modules[__name__], body))
    def do_GET(self):
        self.reply(405, {'success': False, 'results': [], 'error': 'Only POST /search is available'})
    def log_message(self, fmt, *args):
        # Never log the query, note content or authorization header.
        print('HTTP', args[1] if len(args) > 1 else 'request', flush=True)

if __name__ == '__main__':
    token = TOKEN_FILE.read_text().strip()
    if len(token) < 32:
        raise SystemExit('A local token of at least 32 characters is required')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.token = token
    print(f'Read-only search listening at {HOST}:{PORT}', flush=True)
    server.serve_forever()
