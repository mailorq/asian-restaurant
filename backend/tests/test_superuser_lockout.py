"""
two superusers must not be able to demote each other into an empty room

the guard reads the set of active superusers; without locking that set, each transaction sees the
other still active and both commit
"""

import threading

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction

from accounts.roles import NotAuthorized
from employee import service
from tests.concurrency import Writer, queue_behind


@pytest.mark.django_db(transaction=True)
def test_only_one_of_two_concurrent_demotions_can_win(monkeypatch):
    User = get_user_model()
    first = User.objects.create_superuser(username="+79990000201", password="Pass!2345")
    second = User.objects.create_superuser(username="+79990000202", password="Pass!2345")
    holding, release, outcomes = threading.Event(), threading.Event(), {}
    real = service.lock_privileged

    # the first demotion stops holding the superuser rows it read, before it writes anything
    def first_holds_the_superusers(actor, target_pk):
        locked = real(actor, target_pk)
        if threading.current_thread().name == "first":
            holding.set()
            assert release.wait(timeout=20)
        return locked

    monkeypatch.setattr(service, "lock_privileged", first_holds_the_superusers)

    def demote(name, actor, target):
        def run():
            try:
                with transaction.atomic():
                    service.set_superuser(actor=actor, target=target, is_superuser=False)
                outcomes[name] = "committed"
            except NotAuthorized:
                outcomes[name] = "refused"

        return run

    by_first = Writer("first", demote("first", first, second))
    by_second = Writer("second", demote("second", second, first))
    met = queue_behind(by_first, by_second, holding, release)

    assert (by_first.error, by_second.error) == (None, None)
    assert outcomes == {"first": "committed", "second": "refused"}
    left = User.objects.filter(is_superuser=True, is_active=True).values_list("pk", flat=True)
    assert list(left) == [first.pk]
    assert met == "blocked"
    assert 'from "accounts_user"' in by_second.blocked_in.lower()
    assert "for update" in by_second.blocked_in.lower()
