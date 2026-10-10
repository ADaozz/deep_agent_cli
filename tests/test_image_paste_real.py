"""Image ingress regression checks using an actual browser-generated PNG.

Local HTTP tests verify real PTY input and exact model-wire bytes; their fixed
server response is not a real vision-model acceptance result. Native bitmap
clipboard acceptance needs WSL and is reported separately.
"""
from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import struct
import threading
import zlib

import pytest

from agent.attachments import AttachmentStore, image_attachment_from_path
from agent.cli.clipboard import ClipboardAdapter, ClipboardUnavailable, _is_wsl
import tests.test_tui_pty as pty_probe

PNG = Path(__file__).parent / 'fixtures' / 'browser-page.png'


def test_fixture_is_decodable_real_png():
    data = PNG.read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    position = 8
    compressed = bytearray()
    seen_end = False
    while position < len(data):
        size = int.from_bytes(data[position:position + 4], 'big')
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + size]
        crc = int.from_bytes(data[position + 8 + size:position + 12 + size], 'big')
        assert zlib.crc32(kind + payload) & 0xffffffff == crc
        if kind == b'IHDR':
            width, height, depth, color, *_ = struct.unpack('>IIBBBBB', payload)
            assert (width, height, depth, color) == (1280, 720, 8, 2)
        elif kind == b'IDAT':
            compressed.extend(payload)
        elif kind == b'IEND':
            seen_end = True
        position += size + 12
    assert seen_end
    assert len(zlib.decompress(compressed)) == 720 * (1 + 1280 * 3)


def test_real_png_storage_roundtrip(tmp_path):
    image = image_attachment_from_path(PNG)
    store = AttachmentStore(tmp_path / 'attachments')
    ref = store.put(image)
    assert ref.mime_type == 'image/png'
    assert ref.size == PNG.stat().st_size
    assert store.read(ref).data == PNG.read_bytes()


@pytest.fixture
def model_endpoint():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for delta, finish in [({'role': 'assistant', 'content': 'Image bytes received.'}, None), ({}, 'stop')]:
                chunk = {'id': 'image-wire-test', 'object': 'chat.completion.chunk', 'created': 1,
                         'model': 'vision-test', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}/v1', requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.fixture
def image_tui(tmp_path, monkeypatch, model_endpoint, request):
    endpoint, requests = model_endpoint
    config = tmp_path / 'config.yaml'
    config.write_text(f'''llm:
  default: test/vision
  models:
    test:
      api_key: test
      base_url: {endpoint}
      api: chat_completions
      models:
        vision:
          input: [text, image]
sandbox:
  workspace: {tmp_path / 'workspace'}
  allow_unsandboxed: false
''')
    monkeypatch.setenv('DEEP_AGENT_CONFIG', str(config))
    code = pty_probe._PROBE.replace('from agent.config import SandboxConfig', 'from agent.config import SandboxConfig, Settings')
    code = code.replace('config = SandboxConfig(workspace=workspace)', 'config = Settings.load().sandbox')
    code = code.replace('runner = AgentRunner(model=scripted_model(messages), sandbox_config=config)',
                        'runner = AgentRunner(settings=Settings.load(), sandbox_config=config)')
    code = code.replace("'tools':[", "'attachments':[r.to_dict() for r in app.state.attachments],\n                'tools':[")
    entry = getattr(request.node, 'callspec', None)
    entry = entry.params.get('entry') if entry else None
    if entry == 'bitmap_clipboard_adapter_double':
        # This exercises the bitmap branch with a real image, but does not claim
        # a native Windows clipboard: only that OS boundary is substituted.
        replacement = f"""app = CliApplication(runner)
    from agent.cli.clipboard import ClipboardImage
    import shutil
    class FixtureBitmapClipboard:
        exported = 0
        def inspect(self):
            return ClipboardImage()
        def export_image(self):
            self.exported += 1
            target = workspace / 'clipboard-export.png'
            shutil.copyfile({str(PNG.resolve())!r}, target)
            return target
    app.clipboard = FixtureBitmapClipboard()"""
        code = code.replace('app = CliApplication(runner)', replacement)
        code = code.replace("'tools':[", "'clipboard_exports':app.clipboard.exported,\n                'clipboard_export_exists':(workspace/'clipboard-export.png').exists(),\n                'tools':[")
    monkeypatch.setattr(pty_probe, '_PROBE', code)
    tui = pty_probe.Tui(tmp_path)
    try:
        tui.wait(lambda s: s['status'] == 'Ready')
        yield tui, requests
    finally:
        tui.close()


@pytest.mark.sandbox
@pytest.mark.parametrize('entry', ['bracketed_path_paste', 'image_command', 'bitmap_clipboard_adapter_double'])
def test_real_png_pty_attach_submit_exact_wire_bytes(image_tui, entry):
    tui, requests = image_tui
    if entry == 'bracketed_path_paste':
        tui.send('\x1b[200~' + str(PNG.resolve()) + '\x1b[201~')
    elif entry == 'image_command':
        tui.send('/image ' + str(PNG.resolve()) + '\r')
    else:
        tui.send('\x16')
    state = tui.wait(lambda s: len(s['attachments']) == 1)
    assert state['input'] == ''
    if entry == 'bitmap_clipboard_adapter_double':
        assert state['clipboard_exports'] == 1
        assert state['clipboard_export_exists'] is False
    assert state['attachments'][0]['size'] == PNG.stat().st_size
    assert requests == []  # attaching does not send a model request yet
    tui.send('Inspect the attached image.\r')
    state = tui.wait(lambda s: 'Image bytes received.' in s['transcript'])
    assert state['attachments'] == []
    assert requests
    messages = requests[0]['messages']
    blocks = next(m['content'] for m in messages if m['role'] == 'user')
    images = [b for b in blocks if b['type'] == 'image_url']
    assert len(images) == 1
    url = images[0]['image_url']['url']
    assert url.startswith('data:image/png;base64,')
    assert base64.b64decode(url.split(',', 1)[1], validate=True) == PNG.read_bytes()


@pytest.mark.sandbox
def test_real_pty_native_clipboard_unavailable_keeps_draft(image_tui):
    if _is_wsl():
        pytest.skip('This check covers non-WSL unsupported clipboard behavior, not Windows clipboard contents')
    content = ClipboardAdapter().inspect()
    assert isinstance(content, ClipboardUnavailable)
    tui, requests = image_tui
    tui.send('keep this draft\x16')  # Ctrl+V invokes the real adapter, not a fake clipboard
    state = tui.wait(lambda s: 'supported on WSL' in s['status'])
    assert state['input'] == 'keep this draft'
    assert state['cursor'] == len('keep this draft')
    assert state['attachments'] == []
    assert requests == []
