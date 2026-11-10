"""Terminal presentation adapter."""

__all__ = ["CliApplication"]

def __getattr__(name: str):
    if name == "CliApplication":
        from agent.cli.app import CliApplication
        return CliApplication
    raise AttributeError(name)
