"""TEST ONLY: genuine service/gateway/Runner/SQLite/bwrap with a scripted model.
No external LLM is called. All projects and state are temporary. The control
listener is not exposed through the production gateway route whitelist.
"""
from pathlib import Path
import os
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import json
import subprocess
import tempfile
import secrets
import time
import signal


def program(text):
    from langchain_core.messages import AIMessage
    if text=='file-changes':
        operations=[
            ('change-create','write_file',{'file_path':'/workspace/changed.ts','content':'const label = "before";\n'}),
            ('change-edit','edit_file',{'file_path':'/workspace/changed.ts','old_string':'before','new_string':'after'}),
            ('change-overwrite','write_file',{'file_path':'/workspace/changed.ts','content':'const label = "overwritten";\n'}),
            ('change-failed','edit_file',{'file_path':'/workspace/changed.ts','old_string':'does-not-exist','new_string':'should-not-appear'}),
            ('change-execute','execute',{'command':"printf 'artifact stdout\\n'; printf 'artifact stderr\\n' >&2; cat /workspace/changed.ts",'network':False}),
            ('change-delete','delete',{'file_path':'/workspace/changed.ts'}),
        ]
        return [*(AIMessage(content='',tool_calls=[{'id':identity,'name':name,'args':args}]) for identity,name,args in operations),AIMessage(content='文件操作完成')]
    if text=='approval':
        return [AIMessage(content='',tool_calls=[
            {'id':'execute-test','name':'execute','args':{'command':'printf "approved-command\\n"','network':False}},
            {'id':'write-test','name':'write_file','args':{'file_path':'/workspace/example.txt','content':'test-only'}},
        ]),AIMessage(content='批量决定已执行')]
    if text=='single':
        return [AIMessage(content='',tool_calls=[{'id':'single-test','name':'execute','args':{'command':'printf single'}}]),AIMessage(content='single done')]
    if text=='two-approvals':
        return [*program('approval')[:1],AIMessage(content='',tool_calls=[{'id':'second-test','name':'execute','args':{'command':'printf second','network':False}}]),AIMessage(content='second approval done')]
    if text=='human':
        fixture=json.loads((ROOT/'web/e2e/fixtures/human-input.json').read_text())
        request=fixture['run']['result']['human_input']
        return [AIMessage(content='',tool_calls=[{'id':'human-test','name':'request_human_input','args':{
            'reason':request['reason'],'question':request['question'],'fields':request['fields'],
        }}]),AIMessage(content='回答已接收')]
    if text=='slow':
        return [AIMessage(content='',tool_calls=[{'id':'slow-test','name':'execute','args':{
            'command':"/usr/bin/python3 -u -c 'import time; print(\"started\"); time.sleep(30)'",'network':False,
        }}]),AIMessage(content='slow done')]
    if text=='fail':
        def failure():
            raise RuntimeError('controlled model failure')
            yield
        return failure()
    return [AIMessage(content='任务完成：'+text)]


def service(base):
    import uvicorn
    from agent.server.app import create_app
    from agent.server.config import ServerConfig
    from agent.runner import AgentRunner
    from agent.session import SessionStore
    from agent.config import SandboxConfig
    from tests.conftest import scripted_model
    from langchain_core.messages import AIMessage

    class Runner(AgentRunner):
        def submit_human_input(self,values,**callbacks):
            if values.get('name')=='reject-once' and not getattr(self,'rejected_once',False):
                self.rejected_once=True
                raise ValueError('controlled asynchronous rejection')
            return super().submit_human_input(values,**callbacks)

        def invoke(self,text,**callbacks):
            self._chat_model.messages=iter(program(text))
            if text=='pause':
                original=callbacks['on_event']
                def event(item):
                    original(item)
                    if item.type=='run_started':self.request_pause()
                callbacks['on_event']=event
            return super().invoke(text,**callbacks)

    def factory(config,project_id,session_id):
        return Runner(model=scripted_model([AIMessage(content='恢复完成')]),
                      session_store=SessionStore(base/f'{project_id}-checkpoint.sqlite3'),
                      sandbox_config=SandboxConfig(workspace=config.projects[project_id]),thread_id=session_id)

    config=ServerConfig({p:base/p for p in ('app','worker','third')},base/'model.yaml',base/'state',os.environ['DEEP_AGENT_SERVICE_TOKEN'])
    uvicorn.run(create_app(config,factory=factory),host='127.0.0.1',port=8080,log_level='error',access_log=False)


def orchestrate():
    import httpx
    import uvicorn
    from fastapi import FastAPI
    with tempfile.TemporaryDirectory(prefix='deep-agent-web-e2e-') as folder:
        base=Path(folder)
        for p in ('app','worker','third'):(base/p).mkdir()
        env={**os.environ,'PYTHONPATH':str(ROOT),'DEEP_AGENT_SERVICE_TOKEN':secrets.token_urlsafe(32),
             'DEEP_AGENT_SERVICE_BASE_URL':'http://127.0.0.1:8080'}
        env.pop('WEB_DIST_DIR',None)
        children={}
        def start(role):
            command=[sys.executable,str(Path(__file__).resolve()),'service',str(base)] if role=='service' else [sys.executable,'-m','agent.web_gateway.main']
            children[role]=subprocess.Popen(command,cwd=ROOT,env=env)
            port=8080 if role=='service' else 8765
            deadline=time.monotonic()+15
            with httpx.Client(trust_env=False,timeout=.5) as client:
                while time.monotonic()<deadline and children[role].poll() is None:
                    try:
                        path='/health' if role=='service' else '/api/agent/health'
                        if client.get(f'http://127.0.0.1:{port}{path}').status_code==200:return
                    except httpx.HTTPError:pass
                    time.sleep(.03)
            raise RuntimeError(f'E2E {role} did not start')
        def restart(role):
            children[role].kill();children[role].wait(5);start(role)
        control=FastAPI()
        @control.get('/health')
        def health():return {'status':'ok'}
        @control.post('/restart/{role}')
        def reset(role:str):
            if role not in ('service','gateway'):return {'error':'unknown fixture role'}
            restart(role);return {'status':'restarted'}
        @control.post('/audit')
        def audit(payload:dict):
            literal=env['DEEP_AGENT_SERVICE_TOKEN'].encode()
            files=[p for p in (ROOT/'web/dist').rglob('*') if p.is_file()]
            matches=sum(p.read_bytes().count(literal) for p in files)
            matches+=json.dumps(payload,ensure_ascii=False).encode().count(literal)
            return {'matches':matches,'bundle_files':len(files)}
        try:
            start('service');start('gateway')
            uvicorn.run(control,host='127.0.0.1',port=8766,log_level='error',access_log=False)
        finally:
            for process in children.values():
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:process.wait(10)
                    except subprocess.TimeoutExpired:process.kill();process.wait(5)


if __name__=='__main__':
    if len(sys.argv)>1:service(Path(sys.argv[2]))
    else:orchestrate()
