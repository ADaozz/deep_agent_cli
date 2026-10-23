"""MCP diagnostics stay in private rotating files, including logging failures."""
import logging
import stat

from agent.mcp.logging import MCPLogHandler


def test_mcp_background_log_rotation_is_private_and_silent(tmp_path, capsys):
    path = tmp_path / 'mcp.log'
    handler = MCPLogHandler(path, maxBytes=160, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    for i in range(8):
        handler.handle(logging.LogRecord('mcp', logging.INFO, __file__, 1, f'{i}: ' + 'x' * 100, (), None))
    handler.close()
    files = list(tmp_path.glob('mcp.log*'))
    assert len(files) == 3
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    handler.handleError(logging.LogRecord('mcp', logging.ERROR, __file__, 1, 'write failed', (), None))
    assert capsys.readouterr().err == ''
