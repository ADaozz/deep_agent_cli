"""Prove Web Other-value encoding against the actual Runner protocol."""
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage
from agent.runner import AgentRunner
from tests.conftest import scripted_model


def test_web_other_values_round_trip_through_real_runner():
    fields=[{'id':'single','type':'single_select','required':True,'options':[{'value':'known','label':'Known'}]},
            {'id':'multi','type':'multi_select','required':True,'options':[{'value':'known','label':'Known'}]},
            {'id':'flag','type':'boolean','required':True}]
    runner=AgentRunner(model=scripted_model([AIMessage(content='',tool_calls=[{
        'id':'web-other','name':'request_human_input','args':{'reason':'r','question':'q','fields':fields},
    }]),AIMessage(content='accepted')]),backend=StateBackend())
    try:
        assert runner.invoke('ask').status=='waiting_human'
        values={'single':'custom free text','multi':['known','custom free text'],'flag':False}
        assert runner.submit_human_input(values).status=='completed'
        messages=runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        answer=next(m for m in messages if isinstance(m,ToolMessage) and m.tool_call_id=='web-other')
        import json
        assert json.loads(answer.content)==values
    finally: runner.close()
