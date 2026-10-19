import base64
import os
import time

import pytest
from fastapi.testclient import TestClient

from agent.server.app import create_app
from agent.server.config import ServerConfig
from agent.server.files import FileRequestError, directory_files, preview_file


def test_directory_tree_lists_real_entries_and_paginates_without_writes(tmp_path):
    root=tmp_path/'repo';root.mkdir()
    (root/'z-dir').mkdir();(root/'z-dir'/'child.txt').write_text('child')
    (root/'.hidden').write_text('hidden')
    (root/'a.txt').write_text('text')
    (root/'link').symlink_to(tmp_path/'outside')
    first=directory_files(root,limit=2)
    assert [entry['name'] for entry in first['entries']]==['z-dir','.hidden']
    second=directory_files(root,offset=first['next_offset'],limit=2)
    assert [entry['kind'] for entry in second['entries']]==['file','symlink']
    assert second['next_offset'] is None
    assert directory_files(root,'z-dir')['entries']==[{'name':'child.txt','path':'z-dir/child.txt','kind':'file'}]
    assert (root/'a.txt').read_text()=='text'


def test_text_preview_is_escaped_by_client_and_bounded_at_a_utf8_boundary(tmp_path,monkeypatch):
    path=tmp_path/'file.txt';path.write_text('<script>evil()</script>\n你好\n')
    result=preview_file(tmp_path,'file.txt')
    assert result['kind']=='text' and result['content']=='<script>evil()</script>\n你好\n'
    assert not result['truncated']
    monkeypatch.setattr('agent.server.files.TEXT_LIMIT',5)
    path.write_text('你好世界')
    result=preview_file(tmp_path,'file.txt')
    assert result['truncated'] and result['content']=='你'


def test_image_binary_and_symlink_previews(tmp_path):
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1kAAAAASUVORK5CYII=')
    (tmp_path/'image.png').write_bytes(png)
    image=preview_file(tmp_path,'image.png')
    assert image['kind']=='image' and image['data_url'].startswith('data:image/png;base64,')
    (tmp_path/'binary').write_bytes(b'\x00secret')
    binary=preview_file(tmp_path,'binary')
    assert binary['kind']=='binary' and 'content' not in binary
    (tmp_path/'link').symlink_to('/etc/passwd')
    link=preview_file(tmp_path,'link')
    assert link['kind']=='symlink' and link['target']=='/etc/passwd'
    assert 'content' not in link


@pytest.mark.parametrize('path',['../secret','/etc/passwd','nested/../../secret','a\x00b','..\\secret'])
def test_files_reject_paths_outside_workspace(tmp_path,path):
    for read in (directory_files,preview_file):
        with pytest.raises(FileRequestError) as error:
            read(tmp_path,path)
        assert error.value.status==422


def test_symlink_directories_and_replacement_cannot_escape_workspace(tmp_path,monkeypatch):
    root=tmp_path/'repo';root.mkdir()
    (tmp_path/'secret').write_text('NEVER_READ')
    (root/'outside').symlink_to(tmp_path,target_is_directory=True)
    for read,path in ((directory_files,'outside'),(preview_file,'outside/secret')):
        with pytest.raises(FileRequestError) as error: read(root,path)
        assert error.value.status==403
    file=root/'file.txt';file.write_text('before')
    original=os.open
    def replace_before_open(path,flags,*args,**kwargs):
        if path=='file.txt':
            file.unlink();file.symlink_to(tmp_path/'secret')
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr('agent.server.files.os.open',replace_before_open)
    with pytest.raises(FileRequestError) as error: preview_file(root,'file.txt')
    assert error.value.status==403
    assert 'NEVER_READ' not in str(error.value)


def test_missing_files_and_special_files_do_not_block(tmp_path):
    with pytest.raises(FileRequestError) as error: preview_file(tmp_path,'missing')
    assert error.value.status==404
    os.mkfifo(tmp_path/'pipe')
    start=time.monotonic()
    with pytest.raises(FileRequestError) as error: preview_file(tmp_path,'pipe')
    assert error.value.status==422 and time.monotonic()-start<1


def test_file_endpoints_are_authenticated_and_read_only(tmp_path):
    root=tmp_path/'repo';root.mkdir();(root/'space 中文.txt').write_text('hello')
    config=ServerConfig({'app':root},tmp_path/'model.yaml',tmp_path/'state','files-token')
    auth={'Authorization':'Bearer files-token'}
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        assert client.get('/v1/projects/app/files').status_code==401
        assert client.get('/v1/projects/app/file-preview',params={'path':'space 中文.txt'}).status_code==401
        assert client.get('/v1/projects/app/files',headers=auth).json()['entries'][0]['name']=='space 中文.txt'
        assert client.get('/v1/projects/app/file-preview',headers=auth,params={'path':'space 中文.txt'}).json()['content']=='hello'
        assert client.get('/v1/projects/missing/files',headers=auth).status_code==404
        assert client.get('/v1/projects/app/files',headers=auth,params={'path':'../'}).status_code==422
        for method in ('POST','PUT','PATCH','DELETE'):
            for endpoint in ('files','file-preview'):
                assert client.request(method,f'/v1/projects/app/{endpoint}',headers=auth,json={}).status_code==405
    assert (root/'space 中文.txt').read_text()=='hello'
