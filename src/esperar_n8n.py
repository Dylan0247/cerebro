"""Readiness check for service ordering; no credentials or writes."""
import time,urllib.request
for attempt in range(55):
    try:
        with urllib.request.urlopen('http://127.0.0.1:5678/healthz/readiness',timeout=2) as response:
            if response.status==200:
                print('N8N_READY',flush=True)
                break
    except (OSError,urllib.error.URLError):
        pass
    time.sleep(2)
else:
    raise SystemExit('n8n readiness timed out')
