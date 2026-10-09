import sys,os,json,threading,tempfile,base64,hashlib,shutil
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx,yaml
key=sys.stdin.readline().strip()
root=Path('/workspace/deep_agent_cli');sys.path.insert(0,str(root))
import tests.test_tui_pty as probe
source=root/'tests/fixtures/browser-page.png';source_bytes=source.read_bytes();digest=hashlib.sha256(source_bytes).hexdigest()
wire=[]
class Relay(BaseHTTPRequestHandler):
 def do_POST(self):
  try:
   body=self.rfile.read(int(self.headers['Content-Length']))
   payload=json.loads(body)
   meta={'requested_model':payload.get('model'),'image_count':0,'image_metadata':[]}
   for m in payload.get('messages',[]):
    for b in m.get('content',[]) if isinstance(m.get('content'),list) else []:
     if b.get('type')=='image_url':
      u=b['image_url']['url'];data=base64.b64decode(u.split(',',1)[1],validate=True)
      meta['image_count']+=1
      meta['image_metadata'].append({'mime':u.split(';')[0].removeprefix('data:'),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'matches_source':data==source_bytes})
   with httpx.stream('POST','https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions',headers={'Authorization':self.headers['Authorization'],'Content-Type':'application/json'},content=body,timeout=120) as r:
    meta['http_status']=r.status_code;wire.append(meta)
    self.send_response(r.status_code);self.send_header('Content-Type',r.headers.get('Content-Type','application/json'));self.end_headers()
    for chunk in r.iter_bytes():self.wfile.write(chunk);self.wfile.flush()
  except Exception as e:
   wire.append({'error':str(e).replace(key,'[REDACTED]')})
 def log_message(self,*args):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Relay)
threading.Thread(target=server.serve_forever,daemon=True).start()
code=probe._PROBE.replace('from agent.config import SandboxConfig','from agent.config import SandboxConfig, Settings').replace('config = SandboxConfig(workspace=workspace)','config = Settings.load().sandbox').replace('runner = AgentRunner(model=scripted_model(messages), sandbox_config=config)','runner = AgentRunner(settings=Settings.load(), sandbox_config=config)')
code=code.replace("'tools':[", "'attachments':[r.to_dict() for r in app.state.attachments],\n                'tools':[")
probe._PROBE=code
report={'image_file':'tests/fixtures/browser-page.png','source_bytes':len(source_bytes),'source_sha256':digest,'model':'token-plan/auto','mode':'real PTY + bracketed image-file-path paste + real Qwen','native_bitmap_clipboard_status':'BLOCKED: not WSL; no Windows PowerShell clipboard'}
with tempfile.TemporaryDirectory(prefix='deep-agent-live-image-') as folder:
 task=Path(folder);cfg=yaml.safe_load(Path('/tmp/deep-agent-live.yaml').read_text())
 cfg['llm']['models']['token-plan']['base_url']=f'http://127.0.0.1:{server.server_port}/v1'
 cfg['llm']['models']['token-plan']['models']['auto']={'input':['text','image']}
 cfg['sandbox']={'workspace':str(task/'workspace'),'allow_unsandboxed':False}
 config=task/'config.yaml';config.write_text(yaml.safe_dump(cfg))
 os.environ.update({'DEEP_AGENT_CONFIG':str(config),'TOKEN_PLAN_API_KEY':key,'TERM':'xterm-256color'})
 tui=probe.Tui(task)
 try:
  tui.wait(lambda s:s['status']=='Ready')
  tui.send('\x1b[200~'+str(source)+'\x1b[201~')
  state=tui.wait(lambda s:len(s['attachments'])==1)
  report['attached_metadata']=state['attachments'][0]
  report['request_sent_before_submit']=bool(wire)
  tui.send('只根据这张图片，写出左上角黑色大字的英文原文。不要调用任何工具，不要猜测文件名，只回复图中的文字。\r')
  state=tui.wait(lambda s:s['status']=='Ready' and bool(wire),timeout=180)
  report['transcript']=state['transcript'];report['tool_count']=len(state['tools']);report['pending_images_after_success']=len(state['attachments'])
  report['wire_requests']=wire
  report['status']='PASS' if ('persistent' in state['transcript'].lower() and state['attachments']==[] and not state['tools'] and any(m.get('http_status')==200 and m.get('image_count')==1 and all(x['matches_source'] for x in m['image_metadata']) for m in wire)) else 'FAIL'
 except Exception as e:
  report['status']='FAIL';report['reason']=str(e).replace(key,'[REDACTED]')[-3000:];report['wire_requests']=wire
 finally:
  tui.close()
  evidence=root/'docs/reports/evidence'
  (evidence/'live-image-terminal.txt').write_text(tui.log.read_text(errors='replace').replace(key,'[REDACTED]'))
  (evidence/'live-image.json').write_text(json.dumps(report,indent=2).replace(key,'[REDACTED]'))
server.shutdown();server.server_close()
print('Live real-PNG image path paste and Qwen recognition: '+report['status'],flush=True)
