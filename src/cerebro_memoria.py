#!/usr/bin/env python3
"""Validated handoffs; MCP preview is read-only, commit is an operator-only CLI."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import urllib.request
from pathlib import Path

VAULT = Path(os.environ['CEREBRO_VAULT'])
INSTALL = Path(os.environ.get('CEREBRO_DATA_DIR', str(Path.home() / '.local/share/cerebro')))
CONTEXT = ('Arquitectura_Cerebro_IA','Estado_Global','Registro_Decisiones')
ID_PATTERN = r'SES-[0-9]{8}-[A-Z0-9][A-Z0-9_-]{2,60}'
SCHEMA = {'type':'object','additionalProperties':False,'required':['id','started_at','ended_at','author','objective','constraints','changes','evidence','decisions','pending','next_action'], 'properties':{
    'id':{'type':'string','pattern':'^'+ID_PATTERN+'$'},
    'started_at':{'type':'string','description':'ISO 8601 con zona horaria'},
    'ended_at':{'type':'string','description':'ISO 8601 con zona horaria'},
    'author':{'type':'string','minLength':1,'maxLength':200},
    'objective':{'type':'string','minLength':1,'maxLength':2000},
    'constraints':{'type':'array','maxItems':20,'items':{'type':'string','minLength':1,'maxLength':1000}},
    'changes':{'type':'array','maxItems':30,'items':{'type':'object','additionalProperties':False,'required':['description','status','evidence_ids'],'properties':{
        'description':{'type':'string','minLength':1,'maxLength':2000},
        'status':{'type':'string','enum':['realizado','verificado','propuesto']},
        'evidence_ids':{'type':'array','maxItems':10,'items':{'type':'string'}}}}},
    'evidence':{'type':'array','maxItems':30,'items':{'type':'object','additionalProperties':False,'required':['id','kind','reference','result'],'properties':{
        'id':{'type':'string','pattern':'^[A-Za-z0-9_-]{1,40}$'},
        'kind':{'type':'string','enum':['prueba','nota','declaracion_usuario']},
        'reference':{'type':'string','minLength':1,'maxLength':500},
        'result':{'type':'string','minLength':1,'maxLength':2000}}}},
    'decisions':{'type':'array','maxItems':20,'items':{'type':'object','additionalProperties':False,'required':['description','status','source'],'properties':{
        'description':{'type':'string','minLength':1,'maxLength':1500},
        'status':{'type':'string','enum':['confirmada_usuario','propuesta']},
        'source':{'type':'string','minLength':1,'maxLength':500}}}},
    'pending':{'type':'array','maxItems':30,'items':{'type':'string','minLength':1,'maxLength':1500}},
    'next_action':{'type':'string','minLength':1,'maxLength':1500}}}

def digest(value):
    data=value if isinstance(value,bytes) else value.encode('utf-8')
    return hashlib.sha256(data).hexdigest()

def safe_text(value, limit):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('Texto vacío, inválido o excesivo')
    if any(ord(c)<32 and c not in '\n\t' for c in value):
        raise ValueError('Caracteres de control no admitidos')
    if re.search(r'(?i)(bearer\s+[a-z0-9._-]{12,}|-----BEGIN .*PRIVATE KEY|sk-[A-Za-z0-9]{20,}|(?:password|api[_-]?key|token)\s*[:=]\s*\S{8,})',value):
        raise ValueError('Posible secreto: redactarlo antes de guardar')
    return value.strip()

def validate(value, schema=SCHEMA):
    typ=schema['type']
    if typ=='object':
        if not isinstance(value,dict):raise ValueError('Se esperaba objeto')
        props=schema['properties']
        if set(value)-set(props) or set(schema.get('required',[]))-set(value):raise ValueError('Campos faltantes o no admitidos')
        return {k:validate(v,props[k]) for k,v in value.items()}
    if typ=='array':
        if not isinstance(value,list) or len(value)>schema['maxItems']:raise ValueError('Lista inválida o excesiva')
        return [validate(v,schema['items']) for v in value]
    text=safe_text(value,schema.get('maxLength',200))
    if 'enum' in schema and text not in schema['enum']:raise ValueError('Estado o tipo no admitido')
    if 'pattern' in schema and not re.fullmatch(schema['pattern'],text):raise ValueError('Identificador inválido')
    return text

def validate_record(record):
    r=validate(record)
    times=[]
    for name in ('started_at','ended_at'):
        try:t=dt.datetime.fromisoformat(r[name])
        except ValueError:raise ValueError('Fecha ISO 8601 inválida')
        if t.tzinfo is None or t.utcoffset() is None:raise ValueError('La fecha necesita zona horaria')
        times.append(t)
    if times[1]<times[0]:raise ValueError('Fin anterior al inicio')
    if times[1]>dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=5):raise ValueError('Fecha futura no admitida')
    if r['id'][4:12]!=times[0].strftime('%Y%m%d'):raise ValueError('ID y fecha de inicio no coinciden')
    evidence={e['id']:e for e in r['evidence']}
    if len(evidence)!=len(r['evidence']):raise ValueError('IDs de evidencia duplicados')
    for change in r['changes']:
        refs=change['evidence_ids']
        if any(k not in evidence for k in refs):raise ValueError('Referencia de evidencia inexistente')
        if change['status']=='verificado' and (not refs or not any(evidence[k]['kind']=='prueba' for k in refs)):
            raise ValueError('Un cambio verificado requiere evidencia de prueba')
    return r

def context_snapshot():
    result={}
    token=(INSTALL/'token').read_text().strip()
    for note in CONTEXT:
        req=urllib.request.Request('http://127.0.0.1:18767/search',data=json.dumps({'query':note}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=15) as response:data=json.load(response)
        exact=[r for r in data.get('results',[]) if r['file']==note+'.md' and not r.get('truncated')]
        if not data.get('success') or len(exact)!=1:raise ValueError('No se pudo recuperar contexto canónico: '+note)
        result[note]=digest(exact[0]['content'])
    return result

def render(r):
    def lines(items):return '\n'.join('- '+s.replace('\n','\n  ') for s in items) or '- Ninguno registrado.'
    return '\n'.join([
        '---','tipo: sesion','ambito: global','estado: registrado',
        'id: '+r['id'],'iniciado_en: '+json.dumps(r['started_at']),
        'finalizado_en: '+json.dumps(r['ended_at']),
        'actualizado: '+r['ended_at'][:10],
        'fuente: '+json.dumps(r['author'],ensure_ascii=False),'---','',
        '# Relevo '+r['id'],'',
        '> Registro revisado de una sesión. El estado de cada afirmación se indica por separado; guardar esta nota no verifica automáticamente su contenido. Ámbito global, sin proyecto declarado.','',
        '## Objetivo','',r['objective'],'','## Restricciones','',lines(r['constraints']),'',
        '## Cambios y estado','',lines([f"[{c['status']}] {c['description']} — evidencia: {', '.join(c['evidence_ids']) or 'ninguna'}" for c in r['changes']]),'',
        '## Evidencia','',lines([f"{e['id']} [{e['kind']}] {e['reference']}: {e['result']}" for e in r['evidence']]),'',
        '## Decisiones','',lines([f"[{d['status']}] {d['description']} — origen: {d['source']}" for d in r['decisions']]),'',
        '## Pendientes','',lines(r['pending']),'','## Siguiente acción','',r['next_action'],'',
        '## Contexto de continuación','','[[Arquitectura_Cerebro_IA]], [[Estado_Global]], [[Registro_Decisiones]] y [[Memoria_Controlada_IA]].',''])

def prepare(record):
    r=validate_record(record)
    markdown=render(r)
    if len(markdown)>35000 or len(markdown.encode('utf-8'))>100000:raise ValueError('Relevo demasiado largo; dividir o resumir antes de guardar')
    proposal={'version':1,'record':r,'base_context':context_snapshot(),'markdown':markdown}
    proposal['proposal_sha256']=digest(json.dumps(proposal,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    return {'success':True,'saved':False,'warning':'Validación estructural: la evidencia declarada debe revisarse. No se ha escrito memoria.','proposal':proposal}

def check_proposal(p):
    if not isinstance(p,dict) or set(p)!={'version','record','base_context','markdown','proposal_sha256'} or p['version']!=1:
        raise ValueError('Formato de propuesta inválido')
    r=validate_record(p['record'])
    if not isinstance(p['markdown'],str) or len(p['markdown'])>35000 or len(p['markdown'].encode('utf-8'))>100000:raise ValueError('Relevo demasiado largo')
    if p['markdown']!=render(r):raise ValueError('La vista previa fue alterada')
    unsigned={k:v for k,v in p.items() if k!='proposal_sha256'}
    if p['proposal_sha256']!=digest(json.dumps(unsigned,ensure_ascii=False,sort_keys=True,separators=(',',':'))):
        raise ValueError('Propuesta alterada; preparar de nuevo')
    if set(p['base_context'])!=set(CONTEXT) or any(not re.fullmatch('[0-9a-f]{64}',v) for v in p['base_context'].values()):
        raise ValueError('Revisión de contexto inválida')
    return r

def open_sessions():
    root=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in VAULT.parts[1:]+('05_Sesiones',):
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
            os.close(root);root=nxt
        return root
    except BaseException:
        os.close(root);raise

def latest_handoff():
    root=open_sessions()
    candidates=[]
    try:
        with os.scandir(root) as entries:
            for index,entry in enumerate(entries):
                if index>=10000:raise ValueError('Límite de sesiones excedido')
                if not re.fullmatch(r'Sesion_'+ID_PATTERN+r'\.md',entry.name) or not entry.is_file(follow_symlinks=False):continue
                fd=os.open(entry.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
                if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);continue
                with os.fdopen(fd,'rb') as stream:data=stream.read(100001)
                if len(data)>100000:continue
                try:
                    text=data.decode('utf-8');header=text.split('---',2)[1]
                    date=re.search(r'^finalizado_en: (.+)$',header,re.M)
                    ended=dt.datetime.fromisoformat(json.loads(date.group(1)))
                    if ended.tzinfo is None:continue
                    candidates.append((ended,entry.name,text))
                except (ValueError,AttributeError,IndexError):continue
        if not candidates:return {'success':True,'results':[],'message':'No hay relevos registrados con el contrato controlado'}
        ended,name,text=max(candidates,key=lambda x:(x[0],x[1]))
        return {'success':True,'results':[{'file':name,'path':str(VAULT/'05_Sesiones'/name),'content':text,'ended_at':ended.isoformat(),'sha256':digest(text)}]}
    finally:os.close(root)

def commit(p, reviewed_sha256):
    r=check_proposal(p)
    if reviewed_sha256!=p['proposal_sha256']:raise ValueError('Hash de revisión no coincide')
    name='Sesion_'+r['id']+'.md'
    data=p['markdown'].encode('utf-8')
    INSTALL.mkdir(parents=True,exist_ok=True)
    # No caller-supplied destination. O_NOFOLLOW for every path component.
    with (INSTALL/'memory.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        root=open_sessions()
        try:
            try:
                existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
            except FileNotFoundError:existing=None
            if existing is not None:
                if not stat.S_ISREG(os.fstat(existing).st_mode):os.close(existing);raise ValueError('Destino existente no es un archivo regular')
                with os.fdopen(existing,'rb') as stream:before=stream.read(100001)
                if before!=data:raise ValueError('ID ya usado con contenido distinto; no se sobrescribe')
                return {'success':True,'saved':True,'idempotent':True,'file':name,'sha256':digest(data)}
            if p['base_context']!=context_snapshot():raise ValueError('El contexto cambió: revisar y preparar una propuesta nueva')
            # Stage complete content before atomic publication; never overwrite a note.
            temp='.relevo-'+p['proposal_sha256']+'.tmp'
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
            try:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.link(temp,name,src_dir_fd=root,dst_dir_fd=root,follow_symlinks=False)
            finally:
                os.unlink(temp,dir_fd=root)
            return {'success':True,'saved':True,'idempotent':False,'file':name,'sha256':digest(data)}
        finally:os.close(root)

def main():
    parser=argparse.ArgumentParser(description='Operator-only session handoff; no arbitrary destination or commands')
    parser.add_argument('operation',choices=['prepare','commit'])
    parser.add_argument('input')
    parser.add_argument('--reviewed-sha256')
    args=parser.parse_args()
    with open(args.input,encoding='utf-8-sig') as stream:data=json.load(stream)
    if args.operation=='prepare':result=prepare(data)
    else:
        if not args.reviewed_sha256:parser.error('commit requires reviewed hash')
        result=commit(data.get('proposal',data),args.reviewed_sha256)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
