import json
import re
import threading
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.test import Client
from django.utils import timezone

from accounts import challenges
from accounts import service as accounts_service
from accounts.api import ALREADY_REGISTERED, CODE_SENT, WRONG_CODE
from accounts.models import PhoneChallenge
from accounts.roles import StaffRole, set_staff_role
from tests.concurrency import WAIT_SECONDS, Writer, queue_behind

pytestmark = pytest.mark.django_db

FREE, TAKEN = "+380671110041", "+380671110042"
PASSWORD = "Pass!2345word"


@pytest.fixture
def owner():
    return get_user_model().objects.create_user(
        username=TAKEN, phone=TAKEN, password="Pass!2345", first_name="Олег"
    )


def _post(client, path, body):
    return client.post(f"/api/auth/{path}", data=json.dumps(body), content_type="application/json")


def _code(sms_outbox, phone):
    texts = [text for to, text in sms_outbox if to == phone]
    found = re.search(r"\b(\d{6})\b", texts[-1]) if texts else None
    return found.group(1) if found else None


def _register(client, phone, code, name="Иван"):
    return _post(client, "register", {"phone": phone, "password": PASSWORD, "name": name, "code": code})


def _wrong(code):
    return "000000" if code != "000000" else "111111"


def _earlier(phone, seconds):
    # the clock moved on since the codes of this phone were sent
    for challenge in PhoneChallenge.objects.filter(phone=phone):
        PhoneChallenge.objects.filter(pk=challenge.pk).update(
            created_at=challenge.created_at - timedelta(seconds=seconds)
        )


def test_a_number_is_registered_only_with_the_code_sent_to_it(client, sms_outbox):
    sent = _post(client, "register/code", {"phone": "067 111 00 41"})
    code = _code(sms_outbox, FREE)

    wrong = _register(client, FREE, _wrong(code))
    right = _register(client, FREE, code)
    again = _register(Client(), FREE, code)

    assert sent.status_code == 200 and sent.json()["detail"] == CODE_SENT
    assert code is not None
    assert wrong.status_code == 400 and wrong.json()["detail"] == WRONG_CODE
    assert right.status_code == 200 and right.json()["phone"] == FREE
    assert client.get("/api/auth/me").status_code == 200
    assert again.status_code == 400
    assert get_user_model().objects.filter(phone=FREE).count() == 1


def test_a_registered_number_gets_the_answer_a_free_one_gets(client, owner, sms_outbox):
    free = _post(client, "register/code", {"phone": FREE})
    taken = _post(client, "register/code", {"phone": TAKEN})
    free_again = _post(client, "register/code", {"phone": FREE})
    taken_again = _post(client, "register/code", {"phone": TAKEN})

    assert (free.status_code, free.json()) == (taken.status_code, taken.json())
    assert (free_again.status_code, free_again.json().keys()) == (taken_again.status_code, taken_again.json().keys())
    assert free_again.status_code == 429
    # the owner learns of the attempt and no code exists for the caller to use
    assert sms_outbox[-1] == (TAKEN, ALREADY_REGISTERED)
    assert _code(sms_outbox, TAKEN) is None
    for guess in ("000000", "123456"):
        assert _register(client, TAKEN, guess).json()["detail"] == WRONG_CODE
    owner.refresh_from_db()
    assert owner.first_name == "Олег" and owner.check_password("Pass!2345")


def test_a_code_takes_five_tries_and_then_none(client, sms_outbox):
    _post(client, "register/code", {"phone": FREE})
    code = _code(sms_outbox, FREE)

    tries = [_register(client, FREE, _wrong(code)).status_code for _ in range(5)]
    last = _register(client, FREE, code)

    assert tries == [400] * 5
    assert last.status_code == 400
    assert not get_user_model().objects.filter(phone=FREE).exists()


def test_an_expired_code_is_refused(client, sms_outbox):
    _post(client, "register/code", {"phone": FREE})
    PhoneChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    assert _register(client, FREE, _code(sms_outbox, FREE)).status_code == 400


def test_only_the_latest_code_works(client, sms_outbox):
    _post(client, "register/code", {"phone": FREE})
    first = _code(sms_outbox, FREE)
    _earlier(FREE, 61)
    _post(client, "register/code", {"phone": FREE})
    latest = _code(sms_outbox, FREE)

    if first != latest:
        assert _register(client, FREE, first).status_code == 400
    assert _register(client, FREE, latest).status_code == 200


@pytest.mark.parametrize("phone", [FREE, TAKEN], ids=["free number", "registered number"])
def test_a_number_gets_a_new_code_a_minute_later_and_five_a_day(client, owner, sms_outbox, phone):
    statuses = []
    for _ in range(6):
        statuses.append(_post(client, "register/code", {"phone": phone}).status_code)
        _earlier(phone, 61)
        # the limit per address is another test's subject
        cache.clear()
    soon = _post(Client(), "register/code", {"phone": phone})

    assert statuses == [200] * 5 + [429]
    assert soon.status_code == 429 and "завтра" in soon.json()["detail"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("earlier", [0, 4], ids=["first code of the number", "fifth code of the day"])
def test_a_start_waits_for_one_holding_the_number_and_then_is_refused(sms_outbox, earlier):
    now = timezone.now()
    for _ in range(earlier):
        PhoneChallenge.objects.create(
            phone=FREE, purpose=PhoneChallenge.Purpose.REGISTER, code_hash="spent", expires_at=now, used_at=now
        )
    _earlier(FREE, 61)
    holding, release, results = threading.Event(), threading.Event(), {}

    def first():
        with transaction.atomic():
            challenges.start(FREE, PhoneChallenge.Purpose.REGISTER, lambda code: f"code {code}")
            holding.set()
            assert release.wait(WAIT_SECONDS)

    def second():
        results["second"] = _post(Client(), "register/code", {"phone": FREE})

    met = queue_behind(Writer("first", first), Writer("second", second), holding, release)

    assert met == "blocked"
    assert results["second"].status_code == 429
    assert len(sms_outbox) == 1
    assert PhoneChallenge.objects.count() == earlier + 1
    assert PhoneChallenge.objects.filter(used_at__isnull=True).count() == 1


def test_registration_and_recovery_share_the_limits_of_a_number(client, owner, sms_outbox):
    register = _post(client, "register/code", {"phone": TAKEN})
    recover = _post(client, "password/code", {"phone": TAKEN})
    statuses = [register.status_code, recover.status_code]
    for path in ("password/code", "register/code", "password/code", "register/code", "password/code"):
        _earlier(TAKEN, 61)
        cache.clear()
        statuses.append(_post(client, path, {"phone": TAKEN}).status_code)

    assert statuses == [200, 429, 200, 200, 200, 200, 429]
    assert PhoneChallenge.objects.filter(phone=TAKEN).count() == challenges.DAILY_CODES


def test_a_failed_send_keeps_its_place_in_the_limits(client, settings):
    settings.SMS_BACKEND = "tests.sms.FailingSender"

    failed = _post(client, "register/code", {"phone": FREE})
    again = _post(client, "register/code", {"phone": FREE})
    statuses = []
    for _ in range(challenges.DAILY_CODES):
        _earlier(FREE, 61)
        cache.clear()
        statuses.append(_post(client, "register/code", {"phone": FREE}).status_code)

    assert failed.status_code == 503 and again.status_code == 429
    assert statuses == [503] * (challenges.DAILY_CODES - 1) + [429]
    assert PhoneChallenge.objects.filter(phone=FREE).count() == challenges.DAILY_CODES


def test_the_code_itself_is_not_stored(client, sms_outbox):
    _post(client, "register/code", {"phone": FREE})
    code = _code(sms_outbox, FREE)
    challenge = PhoneChallenge.objects.get()

    assert code not in challenge.code_hash and len(challenge.code_hash) == 64


def test_a_registration_without_a_code_is_refused(client):
    response = _post(client, "register", {"phone": FREE, "password": PASSWORD, "name": "Иван"})

    assert response.status_code == 422
    assert not get_user_model().objects.filter(phone=FREE).exists()


def test_a_customer_recovers_access_with_a_code_and_their_other_sessions_end(owner, sms_outbox):
    elsewhere = Client()
    elsewhere.force_login(owner)
    client = Client()

    sent = _post(client, "password/code", {"phone": TAKEN})
    reset = _post(client, "password/reset", {"phone": TAKEN, "code": _code(sms_outbox, TAKEN), "password": PASSWORD})

    assert sent.status_code == 200 and sent.json()["detail"] == CODE_SENT
    assert reset.status_code == 200 and reset.json()["phone"] == TAKEN
    assert client.get("/api/auth/me").status_code == 200
    assert elsewhere.get("/api/auth/me").status_code == 401
    owner.refresh_from_db()
    assert owner.check_password(PASSWORD)


def test_recovery_answers_an_unknown_number_the_same_and_sends_it_nothing(client, owner, sms_outbox):
    unknown = _post(client, "password/code", {"phone": FREE})
    known = _post(client, "password/code", {"phone": TAKEN})
    reset = _post(client, "password/reset", {"phone": FREE, "code": "123456", "password": PASSWORD})

    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
    assert [to for to, _ in sms_outbox] == [TAKEN]
    assert reset.status_code == 400 and reset.json()["detail"] == WRONG_CODE
    assert not get_user_model().objects.filter(phone=FREE).exists()


def test_a_staff_account_is_not_recovered_by_sms(client, superuser, sms_outbox):
    get_user_model().objects.filter(pk=superuser.pk).update(phone="+380671110043")

    sent = _post(client, "password/code", {"phone": "+380671110043"})
    reset = _post(client, "password/reset", {"phone": "+380671110043", "code": "123456", "password": PASSWORD})

    assert sent.status_code == 200 and sent.json()["detail"] == CODE_SENT
    assert sms_outbox == []
    assert reset.status_code == 400
    superuser.refresh_from_db()
    assert superuser.check_password("Pass!2345")


def _recovery_code(sms_outbox):
    assert _post(Client(), "password/code", {"phone": TAKEN}).status_code == 200
    return _code(sms_outbox, TAKEN)


def _untouched(account):
    account.refresh_from_db()
    return account.check_password("Pass!2345")


@pytest.mark.django_db(transaction=True)
def test_a_recovery_waits_for_a_role_grant_holding_the_account_and_then_refuses(owner, superuser, sms_outbox):
    code = _recovery_code(sms_outbox)
    holding, release, results = threading.Event(), threading.Event(), {}

    def grant():
        with transaction.atomic():
            set_staff_role(actor=superuser, target=owner, role=StaffRole.OPERATOR)
            holding.set()
            assert release.wait(WAIT_SECONDS)

    def recover():
        client = Client()
        results["reset"] = _post(client, "password/reset", {"phone": TAKEN, "code": code, "password": PASSWORD})
        results["session"] = client.get("/api/auth/me").status_code

    met = queue_behind(Writer("grant", grant), Writer("recover", recover), holding, release)

    assert met == "blocked"
    assert results["reset"].status_code == 400 and results["reset"].json()["detail"] == WRONG_CODE
    assert results["session"] == 401
    assert _untouched(owner)


@pytest.mark.parametrize("change", ["role grant", "promotion", "deactivation", "phone change"])
def test_a_change_committed_after_the_number_was_looked_up_ends_the_recovery(
    owner, superuser, sms_outbox, monkeypatch, change
):
    code = _recovery_code(sms_outbox)
    looked_up = accounts_service.phone_owner

    def then_changed(number, **kwargs):
        found = looked_up(number, **kwargs)
        if change == "role grant":
            set_staff_role(actor=superuser, target=owner, role=StaffRole.OPERATOR)
        elif change == "promotion":
            get_user_model().objects.filter(pk=owner.pk).update(is_superuser=True, is_staff=True)
        elif change == "deactivation":
            get_user_model().objects.filter(pk=owner.pk).update(is_active=False)
        else:
            # the login moves with the phone, as the profile service moves them
            get_user_model().objects.filter(pk=owner.pk).update(phone="+380671110044", username="+380671110044")
        return found

    monkeypatch.setattr(accounts_service, "phone_owner", then_changed)
    client = Client()

    reset = _post(client, "password/reset", {"phone": TAKEN, "code": code, "password": PASSWORD})

    assert reset.status_code == 400 and reset.json()["detail"] == WRONG_CODE
    assert client.get("/api/auth/me").status_code == 401
    assert _untouched(owner)


def test_a_recovery_code_sets_a_password_once(owner, sms_outbox):
    code = _recovery_code(sms_outbox)

    first = _post(Client(), "password/reset", {"phone": TAKEN, "code": code, "password": PASSWORD})
    second = _post(Client(), "password/reset", {"phone": TAKEN, "code": code, "password": "Other!2345word"})

    assert first.status_code == 200 and second.status_code == 400
    owner.refresh_from_db()
    assert owner.check_password(PASSWORD)


def test_without_a_provider_every_number_gets_the_same_refusal(client, owner, settings):
    settings.SMS_BACKEND = "accounts.sms.DisabledSender"

    answers = {
        (path, phone): _post(client, path, {"phone": phone})
        for path in ("register/code", "password/code")
        for phone in (FREE, TAKEN)
    }

    assert {(r.status_code, r.json()["detail"]) for r in answers.values()} == {
        (503, "Отправка SMS временно недоступна")
    }
    assert not PhoneChallenge.objects.exists()
