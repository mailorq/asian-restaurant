import threading
import time

import psycopg
from django.db import connection

WAIT_SECONDS = 15.0


class Writer(threading.Thread):
    """runs one unit of work on its own pooled connection and records the backend pid serving it"""

    def __init__(self, name: str, work) -> None:
        super().__init__(name=name, daemon=True)
        self.work = work
        self.pid: int | None = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("select pg_backend_pid()")
                self.pid = cursor.fetchone()[0]
            self.work()
        except Exception as exc:
            self.error = exc
        finally:
            connection.close()


def _observer() -> psycopg.Connection:
    db = connection.settings_dict
    params = {
        "dbname": db["NAME"],
        "user": db["USER"],
        "password": db["PASSWORD"],
        "host": db["HOST"],
        "port": db["PORT"],
    }
    return psycopg.connect(
        **{key: value for key, value in params.items() if value}, autocommit=True
    )


def _holds(holder: Writer, holding: threading.Event) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while not holding.wait(0.02):
        if not holder.is_alive() or time.monotonic() > deadline:
            return False
    return True


def _meets(observer: psycopg.Connection, holder: Writer, waiter: Writer) -> str:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if not waiter.is_alive():
            return "finished"
        if waiter.pid is not None:
            blocked = observer.execute(
                "select %s = any(pg_blocking_pids(%s))", (holder.pid, waiter.pid)
            ).fetchone()[0]
            if blocked:
                return "blocked"
        time.sleep(0.02)
    raise AssertionError(
        f"{waiter.name} (pid {waiter.pid}) neither waited on {holder.name} (pid {holder.pid}) "
        "nor finished"
    )


def queue_behind(
    holder: Writer, waiter: Writer, holding: threading.Event, release: threading.Event
) -> str:
    """
    starts waiter once holder signals holding, then releases holder as soon as waiter is blocked by holder's
    backend ("blocked") or done without waiting ("finished"). both threads are joined whatever fails
    """
    # ci runs a pool of two, one slot per writer: the test's own connection goes back first, and the observer connects outside the pool
    connection.close()
    started = []
    try:
        with _observer() as observer:
            holder.start()
            started.append(holder)
            if not _holds(holder, holding):
                raise AssertionError(f"{holder.name} never took its lock: {holder.error!r}")
            waiter.start()
            started.append(waiter)
            return _meets(observer, holder, waiter)
    finally:
        release.set()
        for thread in started:
            thread.join(WAIT_SECONDS)
        running = [thread.name for thread in started if thread.is_alive()]
        assert not running, f"still running after release: {running}"
