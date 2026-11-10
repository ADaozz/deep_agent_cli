"""Public application API with lazy adapter loading."""
from importlib import import_module

_EXPORTS = {
    'ModelProfile': ('agent.models.profile', 'ModelProfile'),
    'BindMount': ('agent.execution.policy', 'BindMount'),
    'SandboxConfig': ('agent.execution.policy', 'SandboxConfig'),
    'RunController': ('agent.runtime.control', 'RunController'),
    'AgentSpec': ('agent.bootstrap.factory', 'AgentSpec'),
    'PreparedAgent': ('agent.bootstrap.factory', 'PreparedAgent'),
    'build_agent': ('agent.bootstrap.factory', 'build_agent'),
    'create_agent': ('agent.bootstrap.factory', 'create_agent'),
    'PermissionMode': ('agent.runtime.permissions', 'PermissionMode'),
    'AgentRunner': ('agent.runtime.runner', 'AgentRunner'),
    'InterruptKind': ('agent.runtime.contracts', 'InterruptKind'),
    'InterruptState': ('agent.runtime.contracts', 'InterruptState'),
    'RunEvent': ('agent.runtime.contracts', 'RunEvent'),
    'RunResult': ('agent.runtime.contracts', 'RunResult'),
    'UnknownInterruptError': ('agent.runtime.contracts', 'UnknownInterruptError'),
    'ExecutionMode': ('agent.execution.types', 'ExecutionMode'),
    'SandboxUnavailableError': ('agent.execution.types', 'SandboxUnavailableError'),
    'ImageAttachment': ('agent.sessions.images', 'ImageAttachment'),
    'ImageAttachmentRef': ('agent.sessions.images', 'ImageAttachmentRef'),
    'SessionStore': ('agent.sessions.store', 'SessionStore'),
    'SessionInfo': ('agent.sessions.contracts', 'SessionInfo'),
    'StopReason': ('agent.sessions.contracts', 'StopReason'),
    'StreamDeltaCallback': ('agent.models.messages', 'StreamDeltaCallback'),
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    value = getattr(import_module(module), symbol)
    globals()[name] = value
    return value
