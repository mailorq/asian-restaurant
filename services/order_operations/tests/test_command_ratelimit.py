import threading
import time

import pytest
import redis
from django.conf import settings
from django.db import connection

from operations import ratelimit
from operations.commands import CommandConflict, create_transition_command
from operations.models import OperationCommand, OperationsOutbox
from operations.ratelimit import LimiterUnavailable, RateLimited


@pytest.fixture(autouse=True)
def limits(settings):
    settings.COMMAND_LIMIT_PER_ACTOR = 10
    settings.COMMAND_LIMIT_PER_ORDER = 3
    settings.COMMAND_LIMIT_WINDOW_SECONDS = 60
    return settings


def _spent(actor_id, order_id):
    store = redis.Redis.from_url(settings.REDIS_URL)
    return [int(value or 0) for value in store.mget(ratelimit.keys(actor_id, order_id))]


def _create(key, order_id=1, target="confirmed"):
    return create_transition_command(
        actor_id=42,
        actor_authz_version=3,
        order_id=order_id,
        expected_status="created",
        target_status=target,
        idempotency_key=key,
    )


def _race(n, work):
    """n threads released at once; each gets none when it went through, or what it raised"""
    barrier = threading.Barrier(n)
    results = [None] * n

    def run(i):
        barrier.wait()
        try:
            work(i)
        except Exception as exc:
            results[i] = exc
        finally:
            # the connection goes back to the pool, which teardown needs with ci's two slots
            connection.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads)
    return results


def test_parallel_new_commands_of_one_employee_pass_exactly_up_to_its_limit():
    results = _race(40, lambda i: ratelimit.charge(42, order_id=i))

    assert results.count(None) == 10
    refused = [r for r in results if r is not None]
    assert all(isinstance(r, RateLimited) and 1 <= r.retry_after <= 60 for r in refused)
    assert sum(_spent(42, i)[1] for i in range(40)) == 10


def test_parallel_new_commands_on_one_order_pass_exactly_up_to_its_limit():
    results = _race(40, lambda i: ratelimit.charge(42, order_id=7))

    assert results.count(None) == 3
    assert _spent(42, 7) == [3, 3]


def test_a_refusal_spends_nothing():
    for _ in range(3):
        ratelimit.charge(42, 7)
    with pytest.raises(RateLimited):
        ratelimit.charge(42, 7)

    assert _spent(42, 7) == [3, 3]


def test_employees_do_not_share_a_limit():
    for order_id in range(10):
        ratelimit.charge(42, order_id)
    with pytest.raises(RateLimited):
        ratelimit.charge(42, 99)

    ratelimit.charge(43, 99)


def test_the_limit_comes_back_after_its_window(limits):
    limits.COMMAND_LIMIT_WINDOW_SECONDS = 1
    for _ in range(3):
        ratelimit.charge(42, 7)
    with pytest.raises(RateLimited) as refused:
        ratelimit.charge(42, 7)
    assert refused.value.retry_after == 1

    time.sleep(1.1)
    ratelimit.charge(42, 7)


@pytest.mark.django_db
def test_a_repeat_of_an_existing_command_is_answered_past_an_exhausted_limit():
    command, _ = _create("k0")
    for order_id in range(100, 109):
        ratelimit.charge(42, order_id)

    again, created = _create("k0")
    with pytest.raises(RateLimited):
        _create("k1", order_id=2)

    assert (again.pk, created) == (command.pk, False)
    assert OperationCommand.objects.count() == OperationsOutbox.objects.count() == 1
    assert _spent(42, 1) == [10, 1]


@pytest.mark.django_db
def test_the_same_key_with_another_intent_is_a_conflict_even_past_the_limit():
    _create("k0")
    for order_id in range(100, 109):
        ratelimit.charge(42, order_id)

    with pytest.raises(CommandConflict):
        _create("k0", target="cancelled")

    assert OperationCommand.objects.count() == 1
    assert _spent(42, 1) == [10, 1]


def test_an_unset_store_refuses_rather_than_counting_nothing(limits):
    limits.REDIS_URL = ""

    with pytest.raises(LimiterUnavailable):
        ratelimit.charge(42, 7)


@pytest.mark.django_db
def test_without_the_store_nothing_is_created_and_an_existing_command_is_still_answered(limits):
    command, _ = _create("k0")
    limits.REDIS_URL = "redis://127.0.0.1:1/0"
    ratelimit._script.cache_clear()

    with pytest.raises(LimiterUnavailable):
        _create("k1", order_id=2)

    assert _create("k0")[0].pk == command.pk
    assert OperationCommand.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_requests_with_one_key_create_one_command_and_never_more_than_charged():
    results = _race(2, lambda i: _create("k0"))

    assert results == [None, None]
    assert OperationCommand.objects.count() == 1
    # both may pass the lookup before either creates: each is then charged, and the loser returns the winner's command
    assert _spent(42, 1)[1] in (1, 2)
