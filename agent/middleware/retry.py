# Technical retry: reuse LangChain ModelRetryMiddleware / ToolRetryMiddleware
# for transient errors (timeout / 503). A user-triggered "run again" is a
# checkpoint resume, not this middleware.
from langchain.agents.middleware.model_retry import default_retry_on

TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


def retry_on_transient(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status in TRANSIENT_HTTP or status >= 500):
        return True
    return default_retry_on(exc)
