"""Persistent, conservative task lifecycle gate, independent of ROS and audio."""
import math
import os
import sqlite3
import time
from dataclasses import dataclass


KINDS = frozenset({"accepted", "navigating", "arrived", "cancelled", "failed"})


@dataclass(frozen=True)
class Event:
    task_id: str
    map_name: str
    point_name: str
    kind: str
    sequence: int

    def __post_init__(self):
        for value in (self.task_id, self.map_name, self.point_name):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError("task_id/map_name/point_name must be nonempty strings <=256 chars")
        if not isinstance(self.kind, str) or self.kind not in KINDS:
            raise ValueError("unknown lifecycle event")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be a nonnegative integer")


class ArrivalGate:
    """One robot/consumer per database. Claim before audio: at-most-once attempts.

    A producer must supply a globally unique task_id for EVERY navigation attempt,
    increasing per-task sequence, and authoritative goal-bound terminal results.
    This class cannot derive those facts from an idle/proximity/status snapshot.
    """

    def __init__(self, database, messages, max_task_seconds=300, clock=time.monotonic):
        if not math.isfinite(max_task_seconds) or max_task_seconds <= 0:
            raise ValueError("max_task_seconds must be positive")
        self.messages = messages
        self.clock = clock
        self.max_task_seconds = max_task_seconds
        self.started = {}
        self.lock = open(str(database) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lock.seek(0, 2)
                if self.lock.tell() == 0:
                    self.lock.write(b"0")
                    self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("another consumer owns this database") from None
        self.db = sqlite3.connect(str(database), isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, map_name TEXT NOT NULL, point_name TEXT NOT NULL,
            state TEXT NOT NULL, sequence INTEGER NOT NULL, audio_result TEXT)""")
        # Do not resume a pending goal after a consumer restart or replay stale success.
        self.db.execute("UPDATE tasks SET state='interrupted' WHERE state IN ('accepted','navigating')")

    def close(self):
        self.db.close()
        self.lock.close()

    def handle(self, event, player):
        """Return a diagnostic string; only claimed arrivals call player.play()."""
        if not isinstance(event, Event):
            raise TypeError("expected validated Event")
        message = None
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT map_name,point_name,state,sequence FROM tasks WHERE task_id=?",
                (event.task_id,),
            ).fetchone()
            if row and (row[0], row[1]) != (event.map_name, event.point_name):
                outcome = "identity_mismatch"
            elif row and (row[2] not in {"accepted", "navigating"} or event.sequence <= row[3]):
                outcome = "duplicate_or_terminal"
            elif event.kind == "accepted" and row:
                outcome = "already_accepted"
            elif event.kind == "accepted":
                self.db.execute("UPDATE tasks SET state='superseded' WHERE state IN ('accepted','navigating')")
                self.started.clear()
                self.started[event.task_id] = self.clock()
                self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,NULL)",
                                (event.task_id, event.map_name, event.point_name, "accepted", event.sequence))
                outcome = "accepted"
            elif event.kind in {"cancelled", "failed"}:
                self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,NULL)",
                                (event.task_id, event.map_name, event.point_name, event.kind, event.sequence))
                self.started.pop(event.task_id, None)
                outcome = event.kind
            elif not row:
                # A terminal event before acceptance must not later become a fresh task.
                if event.kind == "arrived":
                    self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,NULL)",
                                    (event.task_id, event.map_name, event.point_name, "orphan_result", event.sequence))
                outcome = "untracked_task"
            elif self.clock() - self.started.get(event.task_id, float('-inf')) > self.max_task_seconds:
                self.db.execute("UPDATE tasks SET state='expired',sequence=? WHERE task_id=?",
                                (event.sequence, event.task_id))
                self.started.pop(event.task_id, None)
                outcome = "expired"
            elif event.kind == "navigating":
                self.db.execute("UPDATE tasks SET state='navigating',sequence=? WHERE task_id=?",
                                (event.sequence, event.task_id))
                outcome = "navigating"
            elif row[2] != "navigating":
                self.db.execute("UPDATE tasks SET state='unproven_result',sequence=? WHERE task_id=?",
                                (event.sequence, event.task_id))
                self.started.pop(event.task_id, None)
                outcome = "no_navigation_evidence"
            else:
                message = self.messages.get((event.map_name, event.point_name))
                state = "claimed" if message else "unconfigured"
                self.db.execute("UPDATE tasks SET state=?,sequence=? WHERE task_id=?",
                                (state, event.sequence, event.task_id))
                self.started.pop(event.task_id, None)
                outcome = state
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        if message:
            try:
                player.play(message, event.task_id)
            except Exception as exc:
                # A timeout may mean the robot already received the request. Never retry.
                self.db.execute("UPDATE tasks SET audio_result=? WHERE task_id=?",
                                ("failed_or_unknown: " + str(exc)[:500], event.task_id))
                return "audio_failed_or_unknown"
            self.db.execute("UPDATE tasks SET audio_result='submitted' WHERE task_id=?", (event.task_id,))
            return "audio_submitted"
        return outcome
