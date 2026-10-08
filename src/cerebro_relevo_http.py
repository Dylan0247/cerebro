#!/usr/bin/env python3
"""Read-only handoff validation backend. No commit endpoint is exposed."""
import hmac
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import cerebro_memoria as memory

class Handler(BaseHTTPRequestHandler):
    server_version='CerebroProposal/1'
    def reply(self,status,value):
        raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        self.connection.settimeout(15)
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):
            self.reply(401,{'success':False,'saved':False,'error':'Unauthorized'});return
        if self.path!='/prepare':
            self.reply(404,{'success':False,'saved':False,'error':'No endpoint de escritura; solo /prepare'});return
        try:
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':raise ValueError('application/json required')
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=150000:raise ValueError('Body size out of range')
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict) or set(body)!={'record'}:raise ValueError('Solo se admite record')
            result=memory.prepare(body['record'])
        except (ValueError,TypeError) as exc:
            result={'success':False,'saved':False,'error':str(exc)}
        except Exception:
            result={'success':False,'saved':False,'error':'Fallo al recuperar el contexto canónico; comprobar servicio lector'}
        self.reply(200,result)
    def do_GET(self):self.reply(405,{'success':False,'saved':False,'error':'Only POST /prepare'})
    def log_message(self,fmt,*args):
        # No bodies, note content, credentials or questions in the journal.
        print('HTTP',args[1] if len(args)>1 else 'request',flush=True)

if __name__=='__main__':
    token=(memory.INSTALL/'token').read_text().strip()
    if len(token)<32:raise SystemExit('Invalid local credential')
    server=ThreadingHTTPServer(('127.0.0.1',18768),Handler)
    server.token=token
    print('Read-only proposal validation listening on 127.0.0.1:18768',flush=True)
    server.serve_forever()
