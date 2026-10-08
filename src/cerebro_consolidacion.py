#!/usr/bin/env python3
"""Read-only proposals; reviewed operator CLI updates one fixed canonical note."""
import argparse,datetime as dt,difflib,fcntl,hashlib,json,os,re,stat
from pathlib import Path
import cerebro_memoria as memory

TARGETS={n:'01_Global/'+n+'.md' for n in ('Estado_Global','Registro_Decisiones','Objetivos_Globales')}
START='<!-- CEREBRO:CONSOLIDADO:INICIO -->'
END='<!-- CEREBRO:CONSOLIDADO:FIN -->'
ID=r'CON-[0-9]{8}-[A-Z0-9][A-Z0-9_-]{2,50}'
SCHEMA={'type':'object','additionalProperties':False,'required':['id','target','content','reason','evidence'], 'properties':{
 'id':{'type':'string','pattern':'^'+ID+'$'},'target':{'type':'string','enum':list(TARGETS)},
 'content':{'type':'string','minLength':1,'maxLength':8000},'reason':{'type':'string','minLength':1,'maxLength':1000},
 'evidence':{'type':'array','maxItems':10,'items':{'type':'object','additionalProperties':False,'required':['file','sha256'],'properties':{'file':{'type':'string','maxLength':150},'sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'}}}}}}

def sha(data):return hashlib.sha256(data if isinstance(data,bytes) else data.encode()).hexdigest()
def canonical(data):return json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def root_fd():
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in memory.VAULT.parts[1:]:
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        return fd
    except BaseException:os.close(fd);raise

def parent_fd(relative):
    fd=root_fd()
    try:
        for part in Path(relative).parts[:-1]:
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        return fd
    except BaseException:os.close(fd);raise

def read(relative):
    parent=parent_fd(relative)
    try:fd=os.open(Path(relative).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally:os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise ValueError('Fuente no regular')
    with os.fdopen(fd,'rb') as stream:raw=stream.read(100001)
    if len(raw)>100000:raise ValueError('Fuente demasiado larga')
    raw.decode('utf-8');return raw

def evidence_path(name):
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.md',name):raise ValueError('Solo nombres de notas, sin rutas')
    found=[]
    for index,p in enumerate(memory.VAULT.rglob('*.md')):
        if index>=10000:raise ValueError('Demasiadas notas')
        if p.name==name:found.append(str(p.relative_to(memory.VAULT)))
    if len(found)!=1:raise ValueError('Fuente ausente o ambigua')
    return found[0]

def validate(record):
    r=memory.validate(record,SCHEMA)
    if not re.fullmatch(ID,r['id']) or r['target'] not in TARGETS:raise ValueError('Destino o ID inválido')
    try:dt.datetime.strptime(r['id'][4:12],'%Y%m%d')
    except ValueError:raise ValueError('Fecha del ID inválida')
    if r['id'][4:12]>dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d'):raise ValueError('ID futuro')
    if START in r['content'] or END in r['content']:raise ValueError('Marcadores reservados')
    if not r['evidence'] or len({e['file'] for e in r['evidence']})!=len(r['evidence']):raise ValueError('Evidencia requerida y sin duplicados')
    for item in r['evidence']:
        data=read(evidence_path(item['file']))
        if sha(data)!=item['sha256']:raise ValueError('La evidencia cambió: '+item['file'])
        text=data.decode();header=re.match(r'^---\r?\n(.*?)\r?\n---(?:\r?\n|$)',text,re.S)
        state=re.search(r'''(?m)^estado:\s*["']?(vigente|registrado)["']?\s*$''',header.group(1)) if header else None
        if item['file'].startswith('Auto_') or not state:
            raise ValueError('Evidencia debe tener estado vigente o registrado; no usar borradores')
    return r

def render(before,r):
    text=before.decode('utf-8')
    sources=', '.join('[['+e['file'][:-3]+']]' for e in r['evidence'])
    block=START+'\n## Estado consolidado revisado — '+r['id'][4:8]+'-'+r['id'][8:10]+'-'+r['id'][10:12]+'\n\n'+r['content']+'\n\nFuentes revisadas: '+sources+'. Registro: '+r['id']+'.\n'+END
    if START in text or END in text:
        if text.count(START)!=1 or text.count(END)!=1 or text.index(START)>text.index(END):raise ValueError('Sección consolidada inválida')
        a=text.index(START);b=text.index(END)+len(END);return (text[:a]+block+text[b:]).encode()
    if not text.startswith('---\n') and not text.startswith('---\r\n'):raise ValueError('Cabecera Markdown requerida')
    match=re.match(r'^---\r?\n.*?\r?\n---(?:\r?\n)',text,re.S)
    if not match:raise ValueError('Cabecera inválida')
    pos=match.end()
    return (text[:pos]+'\n'+block+'\n\n## Histórico conservado\n\nLos apartados anteriores se conservan por trazabilidad. Para el estado actual prevalece la sección consolidada de arriba.\n\n'+text[pos:]).encode()

def prepare(record):
    r=validate(record);before=read(TARGETS[r['target']]);after=render(before,r)
    if len(after)>100000:raise ValueError('Nota demasiado larga')
    p={'version':1,'record':r,'before_sha256':sha(before),'after_sha256':sha(after),'preview':after.decode(),'diff':''.join(difflib.unified_diff(before.decode().splitlines(True),after.decode().splitlines(True),fromfile=r['target']+' antes',tofile=r['target']+' propuesta'))}
    p['proposal_sha256']=sha(canonical(p))
    return {'success':True,'saved':False,'warning':'La validación no demuestra verdad. Revisar afirmaciones, evidencia y diferencia; solo el operador puede aplicar.', 'proposal':p}

def current_state():
    raw=read(TARGETS['Estado_Global']);text=raw.decode()
    if START not in text or END not in text:return {'success':False,'message':'Aún no hay sección consolidada; leer Estado_Global completo'}
    section=text.split(START,1)[1].split(END,1)[0].strip()
    pending=[]
    if '## Próximos pasos\n' in section:
        pending=[line[2:].strip() for line in section.split('## Próximos pasos\n',1)[1].splitlines() if line.startswith('- ')]
    return {'success':True,'file':'Estado_Global.md','state':'vigente','content':section,'pending':pending,'sha256':sha(raw),'warning':'Sección revisada vigente; para próximos pasos usar pending, no planes históricos de borradores. Contrastar cambios recientes con fuentes.'}

def checked(p,review):
    if not isinstance(p,dict) or set(p)!={'version','record','before_sha256','after_sha256','preview','diff','proposal_sha256'} or p['version']!=1:raise ValueError('Formato inválido')
    digest=sha(canonical({k:v for k,v in p.items() if k!='proposal_sha256'}))
    if digest!=p['proposal_sha256'] or review!=digest:raise ValueError('Revisión inválida o propuesta alterada')
    if sha(p['preview'])!=p['after_sha256']:raise ValueError('Vista previa alterada')
    return validate(p['record'])

def atomic_target(relative,data,expected):
    parent=parent_fd(relative);name=Path(relative).name;temp='.consolidar-'+sha(data)+'.tmp'
    try:
        original=os.stat(name,dir_fd=parent,follow_symlinks=False)
        if not stat.S_ISREG(original.st_mode):raise ValueError('Destino no regular')
        if sha(read(relative))!=expected:raise ValueError('Estado cambió: preparar y revisar de nuevo')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,original.st_mode&0o777,dir_fd=parent)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            if sha(read(relative))!=expected:raise ValueError('Edición concurrente detectada')
            os.replace(temp,name,src_dir_fd=parent,dst_dir_fd=parent);os.fsync(parent)
        finally:
            try:os.unlink(temp,dir_fd=parent)
            except FileNotFoundError:pass
    finally:os.close(parent)

def save_once(path,data):
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise ValueError('Registro no regular')
        with os.fdopen(fd,'rb') as stream:existing=stream.read(200001)
        if existing!=data:raise ValueError('ID de consolidación ya usado')
        return
    with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())

def commit(p,review):
    r=checked(p,review);folder=memory.INSTALL/'consolidaciones';folder.mkdir(mode=0o700,exist_ok=True)
    if folder.is_symlink():raise ValueError('Registro enlazado no permitido')
    with (memory.INSTALL/'consolidacion.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        audit=folder/(r['id']+'.json');backup=folder/(r['id']+'.before.md');target=TARGETS[r['target']]
        current=read(target)
        if sha(current)==p['after_sha256']:
            save_once(audit,canonical(p).encode())
            if not backup.is_file() or backup.is_symlink():raise ValueError('Falta respaldo para confirmar idempotencia')
            return {'success':True,'saved':True,'idempotent':True,'target':r['target']}
        if sha(current)!=p['before_sha256']:raise ValueError('Contexto obsoleto')
        regenerated=prepare(r)['proposal']
        if regenerated!=p:raise ValueError('Vista previa no corresponde al estado')
        save_once(backup,current);save_once(audit,canonical(p).encode())
        atomic_target(target,p['preview'].encode(),p['before_sha256'])
        return {'success':True,'saved':True,'idempotent':False,'target':r['target'],'backup':str(backup),'sha256':p['after_sha256']}

def rollback(p,review):
    r=checked(p,review);backup=memory.INSTALL/'consolidaciones'/(r['id']+'.before.md')
    with (memory.INSTALL/'consolidacion.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if backup.is_symlink():raise ValueError('Respaldo enlazado')
        before=backup.read_bytes()
        if sha(before)!=p['before_sha256']:raise ValueError('Respaldo alterado')
        atomic_target(TARGETS[r['target']],before,p['after_sha256'])
    return {'success':True,'restored':True,'target':r['target']}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['prepare','commit','rollback']);parser.add_argument('input');parser.add_argument('--reviewed-sha256');a=parser.parse_args()
    data=json.loads(Path(a.input).read_text(encoding='utf-8-sig'))
    result=prepare(data) if a.operation=='prepare' else globals()[a.operation](data.get('proposal',data),a.reviewed_sha256)
    print(json.dumps(result,ensure_ascii=False))
