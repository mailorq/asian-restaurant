import threading
import time

from django.db import connection


def in_thread(name, work, errors):
    def run():
        try:
            work()
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    return threading.Thread(target=run, name=name)


def await_a_lock_waiter(timeout=15.0):
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "select count(*) from pg_stat_activity where wait_event_type = 'Lock' "
                "and datname = current_database()"
            )
            if cursor.fetchone()[0]:
                return
            time.sleep(0.05)
    raise AssertionError("the second writer never queued on the locked row")
