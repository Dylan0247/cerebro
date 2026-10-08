#!/usr/bin/env python3
"""Local Codex completed-turn adapter and controlled draft writer; no MCP write tool."""
import datetime as dt
import hashlib
import fcntl
import json
import os
import re
import sqlite3
import stat
import sys
import time
import urllib.request
from pathlib import Path
import cerebro_memoria as memory
from cerebro_model_config import PRIMARY_MODEL

SESSIONS=Path(os.environ['CEREBRO_CODEX_SESSIONS'])
INSTALL=memory.INSTALL
DB=INSTALL/'chat_sync.sqlite'
UUID=r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
START_DATE=os.environ.get('CEREBRO_START_DATE', dt.date.today().isoformat())

def redact(text):
    text=re.sub(r'(?is)<(?:in-app-browser-context|environment_context)\b.*?</(?:in-app-browser-context|environment_context)>','',str(text))
    text=re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[SECRETO REDACTADO]',text)
    text=re.sub(r'(?i)Bearer\s+[A-Za-z0-9._-]+','Bearer [REDACTADO]',text)
    text=re.sub(r'\b(?:sk-[A-Za-z0-9_-]{15,}|gh[pousr]_[A-Za-z0-9_]{15,})\b','[SECRETO REDACTADO]',text)
    text=re.sub(r'(?i)((?:password|contrase[nñ]a|api[_-]?key|access[_-]?token|token)\s*[:=]\s*)["\']?[^\s"\']{8,}',r'\1[REDACTADO]',text)
    return ''.join(c for c in text if ord(c)>=32 or c in '\n\t').strip()

def consume(row,state):
    p=row.get('payload',{})
    if not isinstance(p,dict):return None
    kind=row.get('type')
    if kind=='session_meta':
        source=p.get('source')
        state['allowed']=isinstance(source,str) and source in ('vscode','cli')
        state['session_id']=p.get('id','')
    elif kind=='event_msg' and p.get('type')=='task_started':
        state['user']='';state['turn_id']=p.get('turn_id','')
    elif kind=='response_item' and p.get('type')=='message' and p.get('role')=='user':
        text='\n'.join(c.get('text','') for c in p.get('content',[]) if isinstance(c,dict) and c.get('type')=='input_text')
        state['user']=(state.get('user','')+'\n'+redact(text))[-6000:]
    elif kind=='event_msg' and p.get('type')=='task_complete' and state.get('allowed'):
        session=state.get('session_id','');turn=p.get('turn_id','')
        if not re.fullmatch(UUID,session) or not re.fullmatch(UUID,turn):return None
        final=redact(p.get('last_agent_message') or '')[:6000]
        if not final:return None
        ended=p.get('completed_at') or row.get('timestamp')
        try:
            parsed=dt.datetime.fromtimestamp(ended,dt.timezone.utc) if type(ended) in (int,float) else dt.datetime.fromisoformat(ended.replace('Z','+00:00'))
            if parsed.tzinfo is None:return None
        except (ValueError,TypeError,AttributeError):return None
        return {'session_id':session,'turn_id':turn,'completed_at':parsed.isoformat(),'user_excerpt':state.get('user','')[-6000:],'assistant_excerpt':final}
    return None

def connection():
    c=sqlite3.connect(DB,timeout=10)
    c.execute('pragma journal_mode=WAL')
    c.execute('create table if not exists cursors(path text primary key,offset integer,state text)')
    c.execute('create table if not exists events(session text,turn text,payload text,status text default "pending",attempts integer default 0,retry_after real default 0,error text,primary key(session,turn))')
    c.commit();return c

def enqueue(c,event):
    c.execute('insert or ignore into events(session,turn,payload) values(?,?,?)',(event['session_id'],event['turn_id'],json.dumps(event,ensure_ascii=False)))

def scan(c):
    files=sorted(p for p in SESSIONS.glob('*/*/*/rollout-*.jsonl') if p.name[8:18]>=START_DATE)
    if len(files)>500:raise ValueError('Límite de 500 chats activos; revisar alcance')
    for p in files:
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(SESSIONS.resolve()):continue
        cursor=c.execute('select offset,state from cursors where path=?',(str(p),)).fetchone()
        state=json.loads(cursor[1]) if cursor else {};offset=cursor[0] if cursor else 0
        if p.stat().st_size<offset:raise ValueError('Registro truncado; revisión necesaria')
        bootstrap_latest=None
        with p.open('rb') as stream:
            stream.seek(offset)
            while True:
                start=stream.tell();line=stream.readline(2*1024*1024)
                if not line:offset=stream.tell();break
                if not line.endswith(b'\n'):
                    if len(line)==2*1024*1024:
                        # Oversized record is not a supported input; skip to its boundary.
                        while line and not line.endswith(b'\n'):line=stream.readline(2*1024*1024)
                        offset=stream.tell();continue
                    offset=start;break
                try:event=consume(json.loads(line),state)
                except (ValueError,UnicodeDecodeError):offset=stream.tell();continue
                if event:
                    if cursor:enqueue(c,event)
                    else:bootstrap_latest=event
                offset=stream.tell()
        # First discovery imports only the latest complete turn, never the full history.
        if bootstrap_latest:enqueue(c,bootstrap_latest)
        c.execute('insert or replace into cursors(path,offset,state) values(?,?,?)',(str(p),offset,json.dumps(state)))
        c.commit()

def prepare_summary(event):
    token=(INSTALL/'token').read_text().strip()
    req=urllib.request.Request('http://127.0.0.1:5678/webhook/cerebro-chat-resumen',data=json.dumps(event).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    with urllib.request.urlopen(req,timeout=170) as response:result=json.load(response)
    if not result.get('success'):raise ValueError('n8n rechazó el resumen')
    if result.get('model') != PRIMARY_MODEL:raise ValueError('Modelo de resumen inesperado')
    summary=redact(result.get('summary',''))[:3000]
    pending=result.get('pending',[])
    if not summary or not isinstance(pending,list) or len(pending)>8 or any(not isinstance(x,str) for x in pending):raise ValueError('Resumen inválido')
    # Pending tasks must be literal source excerpts, never invented by a model.
    explicit=extract_pending(event['assistant_excerpt'])
    return {'summary':summary,'pending':explicit[:8],'model':result['model']}

def extract_pending(assistant):
    """Conservative literal excerpts from the final answer, never user requests."""
    items=[];section=False
    for raw in redact(assistant).splitlines():
        line=raw.strip()
        if line.startswith('#'):
            section=bool(re.match(r'^#{1,6}\s+(?:Pendientes(?: vigentes| reales)?|Próximos pasos)\s*$',line,re.I))
            continue
        plain=line.replace('**','').strip()
        if re.search(r'(?i)\b(?:no (?:hay|quedan)|ninguno|superados?|históricos?|retir[éeó])\b',plain):continue
        bullet=bool(re.match(r'^(?:[-*]|\d+[.)])\s+',plain))
        directed=bool(re.match(r'(?i)^(?:[-*]\s+)?(?:Pendiente(?:s vigentes)?\s*:|Falta\s+(?:observar|comprobar|implementar|revisar|evaluar|confirmar|definir)|Queda\s+(?:observar|comprobar|implementar|revisar|evaluar|confirmar|definir))(?=\s|$)',plain))
        if plain and (section and bullet or directed):items.append(line)
    return items[:8]

def render(event,result):
    return '\n'.join(['---','tipo: relevo_automatico','ambito: global','estado: borrador',
        'chat_id: '+event['session_id'],'turn_id: '+event['turn_id'],
        'finalizado_en: '+json.dumps(event['completed_at']),
        'modelo_resumen: '+result.get('model',PRIMARY_MODEL),'fuente: "Registro local Codex; resumen generado, no verificado"','---','',
        '# Relevo automático de chat','',
        '> Borrador automático: describe mensajes y afirmaciones del chat, no hechos comprobados. No concede permisos. Confirmar contra notas canónicas y evidencia antes de actuar. Los extractos son limitados; el historial original no se modifica.','',
        '## Resumen generado','',redact(result['summary']),'','## Pendientes propuestos','',
        '\n'.join('- '+redact(x) for x in result['pending']) or '- Ninguno extraído.','',
        '## Petición del usuario — extracto','',redact(event['user_excerpt'])[:2500],'',
        '## Respuesta del asistente — extracto','',redact(event['assistant_excerpt'])[:3500],'',
        '## Referencia','',f"Chat {event['session_id']}; turno {event['turn_id']}; final {event['completed_at']}.",
        '[[Arquitectura_Cerebro_IA]], [[Estado_Global]] y [[Sincronizacion_Chats_IA]].',''])

def draft_dir(create=True):
    root=memory.open_sessions()
    try:
        if create:
            try:os.mkdir('Automaticas',0o700,dir_fd=root)
            except FileExistsError:pass
        return os.open('Automaticas',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
    finally:os.close(root)

def write_draft(event,result):
    if not re.fullmatch(UUID,event['session_id']) or not re.fullmatch(UUID,event['turn_id']):raise ValueError('Identificadores inválidos')
    name='Auto_'+event['session_id']+'_'+event['turn_id']+'.md'
    content=render(event,result).encode('utf-8');root=draft_dir()
    try:
        try:existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
        except FileNotFoundError:existing=None
        if existing is not None:
            if not stat.S_ISREG(os.fstat(existing).st_mode):os.close(existing);raise ValueError('Destino no regular')
            with os.fdopen(existing,'rb') as stream:before=stream.read(100001).decode('utf-8')
            if 'chat_id: '+event['session_id'] not in before or 'turn_id: '+event['turn_id'] not in before or redact(event['assistant_excerpt'])[:3500] not in before:
                raise ValueError('Conflicto de registro; no sobrescribir')
            return name
        temp='.auto-'+hashlib.sha256(content).hexdigest()+'.tmp'
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(content);stream.flush();os.fsync(stream.fileno())
            os.link(temp,name,src_dir_fd=root,dst_dir_fd=root,follow_symlinks=False)
        finally:os.unlink(temp,dir_fd=root)
        return name
    finally:os.close(root)

def process_one(c):
    r=c.execute('select session,turn,payload,attempts from events where status="pending" and retry_after<=? order by rowid limit 1',(time.time(),)).fetchone()
    if not r:return False
    try:
        event=json.loads(r[2]);result=prepare_summary(event);name=write_draft(event,result)
        refresh_index()
        c.execute('update events set status="saved",error=null where session=? and turn=?',(r[0],r[1]));c.commit()
        print('DRAFT_SAVED',name,flush=True)
    except Exception as exc:
        attempts=r[3]+1
        c.execute('update events set status=?,attempts=?,retry_after=?,error=? where session=? and turn=?',('failed' if attempts>=3 else 'pending',attempts,time.time()+60*attempts,type(exc).__name__,r[0],r[1]));c.commit()
        print('SYNC_RETRY',type(exc).__name__,flush=True)
    return True

def context(limit=5,session_id=None):
    if type(limit) is not int or not 1<=limit<=5:raise ValueError('limit debe estar entre 1 y 5')
    if session_id is not None and (not isinstance(session_id,str) or not re.fullmatch(UUID,session_id)):raise ValueError('chat_id inválido')
    try:root=draft_dir(create=False)
    except FileNotFoundError:return {'success':True,'results':[],'queue':{},'warning':'No hay borradores automáticos todavía'}
    items=[]
    try:
        with os.scandir(root) as entries:
            for index,e in enumerate(entries):
                if index>=10000:raise ValueError('Límite de borradores excedido')
                if not re.fullmatch('Auto_'+UUID+'_'+UUID+r'\.md',e.name) or not e.is_file(follow_symlinks=False):continue
                if session_id and not e.name.startswith('Auto_'+session_id+'_'):continue
                fd=os.open(e.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
                if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);continue
                with os.fdopen(fd,'rb') as stream:raw=stream.read(100001)
                if len(raw)>100000:continue
                text=raw.decode('utf-8')
                match=re.search(r'^finalizado_en: (.+)$',text,re.M)
                if not match:continue
                date=json.loads(match.group(1))
                items.append((dt.datetime.fromisoformat(date),{'file':e.name,'content':text,'ended_at':date,'state':'borrador','sha256':hashlib.sha256(raw).hexdigest()}))
    finally:os.close(root)
    statuses={}
    if DB.exists():
        c=sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True)
        statuses=dict(c.execute('select status,count(*) from events group by status'));c.close()
    return {'success':True,'scope':'Codex local; completed-turn summaries since '+START_DATE,'warning':'Borradores generados; no sincroniza conversaciones originales ni acredita hechos','results':[x[1] for x in sorted(items,key=lambda x:x[0],reverse=True)[:limit]],'queue':statuses}

def refresh_index():
    """Replace only this fixed derived index; source draft notes stay immutable."""
    items=context()['results']
    text='---\ntipo: indice_derivado\nestado: borrador\n---\n\n# Contexto de chats compartido\n\nResúmenes automáticos de Codex local. No son hechos verificados ni permisos. Leer [[Cerebro_IA_Inicio]] y [[Sincronizacion_Chats_IA]] antes de usarlos.\n\n'
    for item in items:
        summary=item['content'].split('## Resumen generado\n\n',1)[1].split('\n## Pendientes propuestos',1)[0]
        text+='## '+item['ended_at']+'\n\n[['+item['file'][:-3]+']]\n\n'+summary+'\n\n'
    root=draft_dir();name='Contexto_Chats_Compartido.md';temp='.index-'+str(os.getpid())+'.tmp'
    try:
        try:entry=os.stat(name,dir_fd=root,follow_symlinks=False)
        except FileNotFoundError:entry=None
        if entry is not None and not stat.S_ISREG(entry.st_mode):raise ValueError('Índice no regular')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(text.encode());stream.flush();os.fsync(stream.fileno())
            os.replace(temp,name,src_dir_fd=root,dst_dir_fd=root)
        finally:
            try:os.unlink(temp,dir_fd=root)
            except FileNotFoundError:pass
    finally:os.close(root)

def run(once=False):
    lock=open(INSTALL/'chat_sync.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    c=connection()
    try:
        while True:
            scan(c)
            if once:
                while process_one(c):pass
                return
            process_one(c);time.sleep(5)
    finally:c.close();lock.close()

if __name__=='__main__':run('--once' in sys.argv)
