"""Workspace-local observation windows, computed from actual tool events."""
import sqlite3
from collections import Counter
from pathlib import Path
from time import time


def update_windows(path: Path, calls: list[tuple]) -> list[dict]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = int(time())
    week = 7 * 86400
    db = sqlite3.connect(path, timeout=1)
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                session TEXT, run TEXT, call TEXT, model TEXT, tool TEXT, outcome TEXT, stamp INTEGER,
                PRIMARY KEY(session, run, call))""")
            db.executemany("""INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session,run,call) DO UPDATE SET outcome=excluded.outcome
                WHERE calls.outcome='unknown' AND excluded.outcome!='unknown'""",
                           [(*row, now) for row in calls])
            db.execute("DELETE FROM calls WHERE stamp < ?", (now - 2 * week,))
            rows = db.execute("""SELECT model, tool, stamp >= ?, outcome, count(*) FROM calls
                GROUP BY model, tool, stamp >= ?, outcome ORDER BY model, tool""", (now-week, now-week))
            groups = {}
            for model, tool, recent, outcome, count in rows:
                groups.setdefault((model, tool, recent), Counter())[outcome] = count
            result = []
            for (model, tool, recent), counts in groups.items():
                known = counts["success"] + counts["failed"]
                result.append(dict(model=model, tool=tool, window="last_7d" if recent else "previous_7d",
                                   calls=sum(counts.values()), success=counts["success"], failed=counts["failed"],
                                   unknown=counts["unknown"], success_rate=counts["success"]/known if known else None))
            return sorted(result, key=lambda row: -row["calls"])[:48]
    finally:
        db.close()
