import json
import logging
import os

from django.test import RequestFactory
from gunicorn.config import Config
from gunicorn.glogging import Logger

from config.jsonlog import JsonFormatter
from config.workers import BoundedUvicornWorker

PHONE = "380671110001"


def test_the_access_line_of_a_search_keeps_its_path_and_not_the_phone(capsys):
    cfg = Config()
    cfg.set("accesslog", "-")
    BoundedUvicornWorker(age=1, ppid=os.getpid(), sockets=[], app=None, timeout=30, cfg=cfg, log=Logger(cfg))

    # the line uvicorn writes for every request
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.5:51000", "GET", f"/api/employee/users?search=%2B{PHONE}&page=1", "1.1", 200
    )
    written = capsys.readouterr().out
    assert '"GET /api/employee/users HTTP/1.1" 200' in written
    assert PHONE not in written


def test_a_logged_request_shows_its_method_and_path_and_not_its_query():
    request = RequestFactory().get("/api/employee/users", {"search": f"+{PHONE}"})
    record = logging.getLogger("django.request").makeRecord(
        "django.request", logging.ERROR, __file__, 1, "Internal Server Error: %s", (request.path,), None,
        extra={"status_code": 500, "request": request},
    )
    rendered = JsonFormatter().format(record)
    assert json.loads(rendered)["request"] == "GET /api/employee/users"
    assert PHONE not in rendered
