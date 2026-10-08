"""Metadata filters over the fixed read-only vault; no model-generated paths."""
import datetime as dt
import hashlib
import json
import re

FIELDS = {'query', 'project', 'state', 'since', 'until', 'exact', 'limit'}
STATES = ('vigente', 'registrado', 'borrador', 'plantilla', 'archivado')

def scalar(text):
    value = text.strip()
    if value.startswith('"'):
        try: return json.loads(value)
        except ValueError: return value.strip('"')
    return value.strip("'")

def metadata(content):
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', content, re.S)
    data = {}
    if match:
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(':')
            if sep and re.fullmatch(r'[a-z_]+', key): data[key] = scalar(value)
    return data

def instant(value, end=False):
    if not isinstance(value, str): raise ValueError('Fecha inválida')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        date = dt.date.fromisoformat(value)
        return dt.datetime.combine(date, dt.time.max if end else dt.time.min, dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None: raise ValueError('Fecha y hora requieren zona horaria')
    return parsed.astimezone(dt.timezone.utc)

def validate(body):
    if not isinstance(body, dict) or set(body) - FIELDS: raise ValueError('Campos no admitidos')
    b = dict(body)
    for key in ('query', 'project'):
        if key in b:
            value = b[key]
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200: raise ValueError('Texto inválido: '+key)
            if any(c in value for c in ('/', '\\', ':', '..')) or any(ord(c)<32 for c in value): raise ValueError('No se admiten rutas ni controles')
            b[key] = value.strip()
    if 'state' in b and b['state'] not in STATES: raise ValueError('Estado no admitido')
    if 'exact' in b and type(b['exact']) is not bool: raise ValueError('exact debe ser booleano')
    if b.get('exact') and 'query' not in b: raise ValueError('exact requiere query')
    if type(b.get('limit', 5)) is not int or not 1 <= b.get('limit',5) <= 20: raise ValueError('limit debe estar entre 1 y 20')
    start = instant(b['since']) if 'since' in b else None
    end = instant(b['until'], True) if 'until' in b else None
    if start and end and start > end: raise ValueError('Intervalo invertido')
    if not any(k in b for k in ('query','project','state','since','until')): raise ValueError('Indicar consulta o filtro')
    return b, start, end

def search(service, body):
    import os
    root = None
    try:
        b, start, end = validate(body)
        needle = service.normalize(re.sub(r'\.md$', '', b.get('query',''), flags=re.I))
        if 'query' in b and not needle: raise ValueError('Consulta vacía normalizada')
        root = os.open(service.VAULT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        paths, errors = service.list_notes(root)
        items = []
        for parts in paths:
            name = service.normalize(parts[-1][:-3])
            if b.get('exact') and name != needle: continue
            try: content = service.read_note(root, parts)
            except (OSError, ValueError) as exc:
                errors.append({'path':'/'.join(parts),'error':str(exc)}); continue
            meta = metadata(content)
            if 'state' in b and meta.get('estado') != b['state']: continue
            if 'project' in b and service.normalize(str(meta.get('proyecto',''))) != service.normalize(b['project']): continue
            date = next((meta[k] for k in ('finalizado_en','actualizado_en','actualizado','registrado_en','iniciado_en') if meta.get(k)), None)
            try: timestamp = instant(str(date)) if date else None
            except ValueError: timestamp = None
            if (start or end) and timestamp is None: continue
            if start and timestamp < start or end and timestamp > end: continue
            rank = 0 if name == needle else 1 if needle and needle in name else 2
            if needle and rank == 2 and needle not in service.normalize(content): continue
            result = {'file':parts[-1], 'path':str(service.VAULT.joinpath(*parts)), 'content':content[:service.MAX_CONTENT],
                'state':meta.get('estado'), 'project':meta.get('proyecto'), 'date':date,
                'date_field':next((k for k in ('finalizado_en','actualizado_en','actualizado','registrado_en','iniciado_en') if meta.get(k)), None),
                'content_sha256':hashlib.sha256(content.encode('utf-8')).hexdigest()}
            if len(content)>service.MAX_CONTENT: result.update(truncated=True,total_chars=len(content))
            items.append((rank, -(timestamp.timestamp() if timestamp else 0), parts, result))
        items.sort(key=lambda x:x[:3])
        chosen=[]; budget=120000
        for _,_,_,item in items[:b.get('limit',5)]:
            if len(item['content'])>budget:
                item['total_chars']=len(item['content']); item['content']=item['content'][:budget]; item['truncated']=True
            chosen.append(item); budget-=len(item['content'])
            if budget<=0: break
        response={'success':not errors,'results':chosen,'total_matches':len(items),'truncated':len(chosen)<len(items),
            'date_policy':'Fechas de cabecera: finalizado_en, actualizado_en, actualizado, registrado_en o iniciado_en. Fechas sin hora se interpretan en UTC; until de solo fecha incluye todo ese día.',
            'hash_policy':'content_sha256 identifica el texto completo decodificado UTF-8; no acredita sus afirmaciones.'}
        if errors: response['errors']=errors[:20]
        return response
    except (OSError, ValueError, TypeError) as exc:
        return {'success':False,'results':[],'error':str(exc)}
    finally:
        if root is not None: os.close(root)
