"""Generic MCP blobs survive provider filtering without enabling sampling."""
from __future__ import annotations

import asyncio
import base64

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.content import create_file_block
from deepagents.backends import StateBackend
from fastmcp import FastMCP
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, ResourceLink, TextContent

from agent.mcp.content import preserve_generic_resources
from agent.mcp.manager import MCPManager
from agent.runner import AgentRunner
from tests.conftest import scripted_model
from tests.mcp_fixture import HttpFixture, text_of


@pytest.mark.parametrize('raw', [b'', b'Resource 42 content', '资源内容\n第二行'.encode()])
def test_generic_utf8_blob_becomes_readable_text(raw):
    block = {'type': 'file', 'base64': base64.b64encode(raw).decode(), 'mime_type': 'application/octet-stream'}
    converted = preserve_generic_resources([block])[0]
    assert converted['type'] == 'text'
    assert 'Encoding: UTF-8' in converted['text']
    assert converted['text'].endswith(raw.decode())
    assert block['type'] == 'file'  # Never mutate the upstream result.


@pytest.mark.parametrize('raw', [b'\x00\x01\xff\x80', b'PK\x03\x04hello', b'\x1b[31m'])
def test_generic_binary_preserves_exact_bytes_as_base64(raw):
    encoded = base64.b64encode(raw).decode()
    converted = preserve_generic_resources([create_file_block(base64=encoded, mime_type='application/octet-stream')])[0]
    assert 'Encoding: Base64' in converted['text']
    assert base64.b64decode(converted['text'].split('\n')[-1], validate=True) == raw


def test_native_media_links_artifacts_and_order_are_preserved():
    image = {'type': 'image', 'base64': 'image-data', 'mime_type': 'image/png'}
    pdf = create_file_block(base64='pdf-data', mime_type='application/pdf')
    link = create_file_block(url='test://static/resource/42', mime_type='application/octet-stream')
    artifact = {'structured_content': {'resource': 42}}
    blocks = [image, create_file_block(base64='YWJj', mime_type='application/octet-stream'), pdf, link]
    converted, returned_artifact = preserve_generic_resources((blocks, artifact))
    assert [b['type'] for b in converted] == ['image', 'text', 'file', 'file']
    assert converted[0] is image and converted[2] is pdf and converted[3] is link
    assert returned_artifact is artifact


def test_malformed_base64_is_preserved_with_an_explicit_diagnostic():
    converted = preserve_generic_resources([create_file_block(base64='invalid!!', mime_type='application/octet-stream')])[0]
    assert 'Invalid Base64' in converted['text']
    assert converted['text'].endswith('invalid!!')


@pytest.fixture
def resource_server():
    def server_factory(auth):
        server = FastMCP('generic-resource-fixture', auth=auth)

        @server.tool
        def resource() -> CallToolResult:
            return CallToolResult(content=[
                TextContent(type='text', text='Returning Resource 42:'),
                EmbeddedResource(type='resource', resource=BlobResourceContents(
                    uri='test://static/resource/42', mime_type='application/octet-stream',
                    blob=base64.b64encode('实际资源内容'.encode()).decode(),
                )),
                EmbeddedResource(type='resource', resource=BlobResourceContents(
                    uri='test://static/resource/binary', mime_type='application/octet-stream',
                    blob=base64.b64encode(b'\x00\xff\x01').decode(),
                )),
                ImageContent(type='image', data='iVBORw0KGgo=', mime_type='image/png'),
                ResourceLink(type='resource_link', uri='test://static/resource/42', name='Resource 42', mime_type='application/octet-stream'),
            ], structured_content={'resource': 42})

        return server

    fixture = HttpFixture(server_factory=server_factory)
    try:
        yield fixture
    finally:
        fixture.close()


def test_real_mcp_resource_reaches_model_after_media_filter(tmp_path, resource_server, monkeypatch):
    manager = MCPManager(tmp_path / 'mcp.json', call_timeout=8, connect_timeout=5)
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'id': 'resource-call', 'name': 'mcp__local__resource', 'args': {}}]),
        AIMessage(content='resource read'),
    ])
    received = []
    original = type(model)._generate

    def record(self, messages, **kwargs):
        received.extend(m for m in messages if isinstance(m, ToolMessage))
        return original(self, messages, **kwargs)

    monkeypatch.setattr(type(model), '_generate', record)
    runner = None
    try:
        assert manager.add_from_fields(name='local', transport='streamable_http', url=resource_server.url).ok
        runner = AgentRunner(model=model, backend=StateBackend(), mcp_manager=manager)
        assert runner.invoke('read Resource 42').status == 'waiting_confirmation'
        assert runner.approve_tool('resource-call').status == 'completed'
        assert received
        content = received[-1].content
        assert '实际资源内容' in text_of(content)
        assert 'AP8B' in text_of(content)
        assert 'was not attached' not in text_of(content)
        assert any(isinstance(b, dict) and b['type'] == 'image' for b in content)
        assert 'test://static/resource/42' in text_of(content)
        assert received[-1].artifact['structured_content'] == {'resource': 42}
        tool = next(t for t in manager.get('local').tools if t.name == 'mcp__local__resource')
        asynchronous = asyncio.run(tool.ainvoke({}))
        assert '实际资源内容' in text_of(asynchronous)
    finally:
        if runner is not None:
            runner.close()
        else:
            manager.close()
