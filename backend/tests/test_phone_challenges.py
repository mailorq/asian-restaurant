import json
import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from accounts.api import ALREADY_REGISTERED, CODE_SENT, WRONG_CODE
from accounts.models import PhoneChallenge

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
