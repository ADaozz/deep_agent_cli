import os,sys,json,threading,tempfile,shutil,hashlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx,yaml
key=sys.stdin.readline().strip()
root=Path('/workspace/deep_agent_cli')
sys.path.insert(0,str(root))
class Relay(BaseHTTPRequestHandler):
 def do_POST(self):
  try:
   payload=self.rfile.read(int(self.headers['Content-Length']))
   with httpx.stream('POST','https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions',headers={'Authorization':self.headers['Authorization'],'Content-Type':'application/json'},content=payload,timeout=120) as r:
    self.send_response(r.status_code);self.send_header('Content-Type',r.headers.get('Content-Type','application/json'));self.end_headers()
    for chunk in r.iter_bytes():self.wfile.write(chunk);self.wfile.flush()
  except Exception:pass
 def log_message(self,*args):pass
class Page(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<html><head><title>Live Skill Test</title></head><body><h1>Live Skill Verified</h1><button>Test button</button></body></html>')
 def log_message(self,*args):pass
relay=ThreadingHTTPServer(('127.0.0.1',0),Relay); page=ThreadingHTTPServer(('127.0.0.1',0),Page)
for s in [relay,page]:threading.Thread(target=s.serve_forever,daemon=True).start()
import tests.test_tui_pty as probe
from agent.config import Settings
code=probe._PROBE.replace('from agent.config import SandboxConfig','from agent.config import SandboxConfig, Settings').replace('config = SandboxConfig(workspace=workspace)','config = Settings.load().sandbox').replace('runner = AgentRunner(model=scripted_model(messages), sandbox_config=config)','runner = AgentRunner(settings=Settings.load(), sandbox_config=config)')
code=code.replace("'is_error':b.is_error}","'is_error':b.is_error,'arguments':b.arguments}")
probe._PROBE=code
report={'model':'token-plan/auto','terminal':'real PTY','browser':'existing system Chromium, headless','model_transport':'temporary loopback relay through configured cloud proxy'}
with tempfile.TemporaryDirectory(prefix='deep-agent-live-pw-') as folder:
 task=Path(folder);home=task/'home';home.mkdir()
 source=Path('/tmp/deep-agent-npm/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/tools/skills/playwright-cli')
 target=home/'.deep-agent/skills/playwright-cli';target.parent.mkdir(parents=True);shutil.copytree(source,target)
 def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
 report['official_skill_sha256']=digest(source/'SKILL.md')
 report['official_skill_copy_identical']=all(digest(p)==digest(target/p.relative_to(source)) for p in source.rglob('*') if p.is_file())
 cfg=yaml.safe_load(Path('/tmp/deep-agent-live.yaml').read_text())
 cfg['llm']['models']['token-plan']['base_url']=f'http://127.0.0.1:{relay.server_port}/v1'
 cfg['sandbox']={'workspace':str(task/'workspace'),'allow_unsandboxed':False,'extra_read_only_mounts':[
  {'source':'/tmp/deep-agent-npm','destination':'/opt/npm-prefix'},
  {'source':str(Path(shutil.which('node')).resolve().parent.parent),'destination':'/opt/node'},
  {'source':'/etc/fonts','destination':'/etc/fonts'}],
  'env_set':{'PATH':'/opt/npm-prefix/bin:/opt/node/bin:/usr/local/bin:/usr/bin:/bin'}}
 config=home/'.deep-agent/config.yaml';config.write_text(yaml.safe_dump(cfg))
 os.environ.update({'HOME':str(home),'DEEP_AGENT_CONFIG':str(config),'TOKEN_PLAN_API_KEY':key,'TERM':'xterm-256color'})
 tui=probe.Tui(task)
 workspace=tui.workspace
 (workspace/'browser-test.json').write_text(json.dumps({'browser':{'browserName':'chromium','launchOptions':{'executablePath':'/usr/lib/chromium/chromium','headless':True}}}))
 try:
  tui.wait(lambda s:s['status']=='Ready')
  tui.send('/permission allow\r');tui.wait(lambda s:s['interaction']=='permission_confirm');tui.send('ALLOW\r')
  tui.wait(lambda s:s['permission']=='allow' and s['pending'] is None)
  tui.send('/skill\r');tui.wait(lambda s:s['interaction']=='skill');tui.send('\r')
  tui.wait(lambda s:s['interaction'] is None and 'playwright-cli' in s['input'])
  prompt=(f' 使用这个官方 Skill 检查 http://127.0.0.1:{page.server_port}/ 。先读取 Skill 说明。已有 playwright-cli 和系统 Chromium；浏览器配置在 /workspace/browser-test.json。'
   '无需安装任何依赖，不修改配置。依次通过独立 execute 调用 playwright-cli open URL --config=browser-test.json、snapshot、eval "document.title"、screenshot、close。'
   '使用无头模式。请务必执行到 close，逐条根据工具状态判断成功，不要只根据文字回答。')
  tui.send(prompt+'\r')
  state=tui.wait(lambda s: any(t['name']=='execute' and 'playwright-cli close' in str(t.get('arguments')) and t['exit_code'] is not None for t in s['tools']),timeout=240)
  tui.wait(lambda s:s['status']=='Ready',timeout=30)
  state=json.loads(tui.state.read_text());report['tools']=state['tools']
  report['skill_read']=any(t['name']=='read_file' and '/skills/playwright-cli/SKILL.md' in str(t.get('arguments')) for t in state['tools'])
  executions=[t for t in state['tools'] if t['name']=='execute']
  report['all_execute_exit_zero']=bool(executions) and all(t['exit_code']==0 and not t['is_error'] for t in executions)
  report['commands_covered']={c:any('playwright-cli '+c in str(t.get('arguments')) for t in executions) for c in ['open','snapshot','eval','screenshot','close']}
  report['status']='PASS' if report['skill_read'] and report['all_execute_exit_zero'] and all(report['commands_covered'].values()) else 'FAIL'
 except Exception as e:
  report['status']='FAIL';report['reason']=str(e).replace(key,'[REDACTED]')[-3000:]
 finally:
  tui.close()
  evidence=root/'docs/reports/evidence'
  (evidence/'live-playwright-terminal.txt').write_text(tui.log.read_text(errors='replace').replace(key,'[REDACTED]'))
  for p in (workspace/'.playwright-cli').glob('*.png'):
   shutil.copyfile(p,evidence/'playwright'/('live-'+p.name))
  report['official_skill_copy_still_identical']=all(digest(p)==digest(target/p.relative_to(source)) for p in source.rglob('*') if p.is_file())
  (evidence/'live-playwright.json').write_text(json.dumps(report,indent=2).replace(key,'[REDACTED]'))
for s in [relay,page]:s.shutdown();s.server_close()
print('Live official Skill + Qwen + real PTY Playwright: '+report['status'],flush=True)
