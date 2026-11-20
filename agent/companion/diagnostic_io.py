"""Deadlines shared by the async RPC and its synchronous evidence readers."""

from contextlib import contextmanager
import sqlite3
from threading import Event
from time import monotonic


class QueryBudget:
    def __init__(self, seconds):
        self.deadline = monotonic() + seconds
        self.cancelled = Event()

    def expired(self):
        return self.cancelled.is_set() or monotonic() >= self.deadline

    def check(self):
        if self.expired():
            raise TimeoutError("Diagnostic query expired")


@contextmanager
def readonly_database(path, budget=None):
    if budget:
        budget.check()
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25)
    try:
        if budget:
            db.set_progress_handler(budget.expired, 1000)
        yield db
    except sqlite3.Error:
        if budget:
            budget.check()
        raise
    finally:
        db.close()
