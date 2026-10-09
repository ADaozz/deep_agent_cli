import sys,os,json,threading,subprocess,tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
key=sys.stdin.readline().strip()
root=Path('/workspace/deep_agent_cli')
class Relay(BaseHTTPRequestHandler):
    def do_POST(self):
        payload=self.rfile.read(int(self.headers['Content-Length']))
        try:
            with httpx.stream('POST','https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions',
                headers={'Authorization':self.headers['Authorization'],'Content-Type':'application/json'},
                content=payload,timeout=120) as r:
                self.send_response(r.status_code)
                self.send_header('Content-Type',r.headers.get('Content-Type','application/json'))
                self.end_headers()
                for chunk in r.iter_bytes():
                    self.wfile.write(chunk); self.wfile.flush()
        except Exception:
            pass
    def log_message(self,*args): pass
server=ThreadingHTTPServer(('127.0.0.1',0),Relay)
threading.Thread(target=server.serve_forever,daemon=True).start()
config=Path('/tmp/deep-agent-live-relay.yaml')
config.write_text(Path('/tmp/deep-agent-live.yaml').read_text().replace(
 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',f'http://127.0.0.1:{server.server_port}/v1'))
env={**os.environ,'TOKEN_PLAN_API_KEY':key,'DEEP_AGENT_CONFIG':str(config),'TERM':'xterm-256color'}
try:
    result=subprocess.run([sys.executable,'examples/e2e_live_smoke.py'],env=env,
                           stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300,cwd=root)
    (root/'docs/reports/evidence/live-smoke-relay.txt').write_text(result.stdout.decode(errors='replace').replace(key,'[REDACTED]')+f'\nProcess exit code: {result.returncode}\n')
    print(f'Live smoke through configured proxy relay exit: {result.returncode}',flush=True)
except subprocess.TimeoutExpired as exc:
    (root/'docs/reports/evidence/live-smoke-relay.txt').write_text((exc.stdout or b'').decode(errors='replace').replace(key,'[REDACTED]')+'\nBLOCKED: watchdog expired\n')
    print('BLOCKED: watchdog expired',flush=True)
finally:
    server.shutdown(); server.server_close()
