"""requests stack_smoke.sh sends from inside the project network, as the tls ingress would forward them"""

import json
import os
import re
import socket
import sys
import time
import urllib.parse
import uuid
from html.parser import HTMLParser

HOST = "smoke.invalid"
MIB = 1024 * 1024
PHOTO_LIMIT = 5 * MIB
SECURITY_HEADERS = (
    "content-security-policy",
    "x-content-type-options",
    "referrer-policy",
)
# the classic one pixel gif: decoders stop at its trailer, so zero padding keeps it a valid photo of any size
PIXEL = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c000000000100010000020244"
    "01003b"
)


def send(
    method, path, body=b"", headers=None, proto="https", host="frontend", port=8080
):
    """http/1.0 over a raw socket: no chunked replies, and a body nginx refuses early is still answered"""
    lines = [
        f"{method} {path} HTTP/1.0",
        f"Host: {HOST}",
        f"X-Forwarded-Proto: {proto}",
    ]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    with socket.create_connection((host, port), timeout=60) as conn:
        try:
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        except OSError:
            pass
        raw = b""
        while chunk := conn.recv(65536):
            raw += chunk
    head, _, content = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    replied = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        replied.setdefault(name.strip().lower(), []).append(value.strip())
    return int(status_line.split()[1]), replied, content


def edge():
    problems = []

    def expect(what, ok):
        if not ok:
            problems.append(what)

    for path in ("/", "/api/health", "/admin/login/", "/static/admin/css/base.css"):
        status, headers, body = send("GET", path)
        expect(f"{path} answered {status}", status == 200)
        missing = [h for h in SECURITY_HEADERS if h not in headers]
        expect(f"{path} lacks {missing}", not missing)
    expect("the spa shell is not served", b'<div id="root"' in send("GET", "/")[2])
    expect(
        "/api/health is not ok",
        json.loads(send("GET", "/api/health")[2]).get("status") == "ok",
    )
    status, _, body = send("GET", "/media/edge-probe.txt")
    expect(f"/media answered {status}", status == 200 and body.strip() == b"edge probe")

    for path, want in (
        ("/assets", "/assets/"),
        ("/admin/", "/admin/login/?next=/admin/"),
    ):
        status, headers, _ = send("GET", path)
        location = headers.get("location", [""])[0]
        expect(
            f"{path} redirects with {status} to {location!r}",
            status in (301, 302) and location == want,
        )
    # the ingress forwarded plain http: the backend sends the client to https on the public host, not to this hop
    status, headers, _ = send("GET", "/api/health", proto="http")
    location = headers.get("location", [""])[0]
    expect(
        f"plain http redirects with {status} to {location!r}",
        location == f"https://{HOST}/api/health",
    )

    for path, limit in (("/api/auth/login", MIB), ("/admin/login/", 6 * MIB)):
        at = send(
            "POST", path, b"x" * limit, {"Content-Type": "application/octet-stream"}
        )[0]
        past = send(
            "POST",
            path,
            b"x" * (limit + 1),
            {"Content-Type": "application/octet-stream"},
        )[0]
        expect(
            f"{path}: {limit} bytes answered {at}, one more {past}",
            at != 413 and past == 413,
        )
    print(json.dumps(problems))


class Form(HTMLParser):
    """the fields a browser submits for the admin change form"""

    def __init__(self):
        super().__init__()
        self.fields, self._select, self._textarea = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name")
        if (
            tag == "input"
            and name
            and a.get("type") not in ("submit", "file", "button")
        ):
            if a.get("type") != "checkbox" or "checked" in a:
                self.fields.append(
                    [
                        name,
                        a.get("value") or ("on" if a.get("type") == "checkbox" else ""),
                    ]
                )
        elif tag == "select" and name:
            self._select = name
        elif tag == "option" and self._select and "selected" in a:
            self.fields.append([self._select, a.get("value") or ""])
        elif tag == "textarea" and name:
            self._textarea = [name, ""]
            self.fields.append(self._textarea)

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self._textarea[1] += data.lstrip("\n")


class Session:
    def __init__(self):
        self.cookies = {}

    def send(self, method, path, body=b"", headers=None):
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        referer = {"Referer": f"https://{HOST}{path}", "Origin": f"https://{HOST}"}
        status, replied, content = send(
            method, path, body, {**referer, "Cookie": cookie, **(headers or {})}
        )
        for header in replied.get("set-cookie", []):
            name, _, value = header.split(";", 1)[0].partition("=")
            self.cookies[name] = value
        return status, replied, content

    def login(self, password):
        _, _, page = self.send("GET", "/admin/login/")
        token = (
            re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page)
            .group(1)
            .decode()
        )
        form = f"csrfmiddlewaretoken={token}&username=%2B79990001234&password={password}&next=/admin/"
        status, headers, _ = self.send(
            "POST",
            "/admin/login/",
            form.encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if (status, headers.get("location")) != (302, ["/admin/"]):
            raise SystemExit(
                json.dumps([f"admin login answered {status} {headers.get('location')}"])
            )

    def upload(self, url, size):
        page = self.send("GET", url)[2].decode()
        parser = Form()
        parser.feed(page)
        boundary = uuid.uuid4().hex
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in parser.fields + [["_save", "Save"]]
        )
        photo = PIXEL + b"\0" * (size - len(PIXEL))
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="photo.gif"\r\nContent-Type: image/gif\r\n\r\n'.encode()
        body += photo + f"\r\n--{boundary}--\r\n".encode()
        return self.send(
            "POST",
            url,
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )


def upload(product):
    admin = Session()
    admin.login(os.environ["SMOKE_ADMIN_PASSWORD"])
    url = f"/admin/menu/product/{product}/change/"
    problems = []
    status, _, _ = admin.upload(url, PHOTO_LIMIT)
    stored = re.search(
        r'href="(/media/products/[^"]+)"', admin.send("GET", url)[2].decode()
    )
    served = send("GET", stored.group(1)) if stored else (0, {}, b"")
    if status != 302 or served[0] != 200 or len(served[2]) != PHOTO_LIMIT:
        problems.append(
            f"a photo at the limit: saved {status}, served {served[0]} with {len(served[2])} bytes"
        )
    status, _, page = admin.upload(url, PHOTO_LIMIT + 1)
    if status != 200 or "Фото больше 5 МБ" not in page.decode():
        problems.append(
            f"a photo one byte past the limit answered {status} without the form error"
        )
    status = admin.upload(url, 6 * MIB + 1)[0]
    if status != 413:
        problems.append(f"a body past the admin limit answered {status}")
    print(json.dumps(problems))


def command_plane(order_id, phone):
    """one status change from the employee panel, through operations, the broker and the storefront, back as an outcome"""
    panel = Session()
    panel.send("GET", "/api/auth/csrf")

    def call(method, path, body=None, headers=None):
        sent = {
            "Content-Type": "application/json",
            "X-CSRFToken": panel.cookies.get("csrftoken", ""),
        }
        payload = json.dumps(body).encode() if body is not None else b""
        status, replied, content = panel.send(
            method, path, payload, {**sent, **(headers or {})}
        )
        return status, replied, json.loads(content) if content else None

    password = os.environ["SMOKE_EMPLOYEE_PASSWORD"]
    status, _, _ = call(
        "POST", "/api/auth/login", {"phone": phone, "password": password}
    )
    if status != 200:
        raise SystemExit(json.dumps([f"the employee login answered {status}"]))
    problems = []
    path = f"/api/employee/orders/{order_id}/transition-commands"
    key = {"Idempotency-Key": f"smoke-{uuid.uuid4().hex}"}
    intent = {
        "expected_status": "created",
        "target_status": "confirmed",
        "reason": "smoke",
    }
    status, headers, command = call("POST", path, intent, key)
    if status != 202:
        raise SystemExit(json.dumps([f"the command answered {status}: {command}"]))
    deadline = time.monotonic() + 120
    while (
        command["status"] in ("pending", "dispatched") and time.monotonic() < deadline
    ):
        time.sleep(2)
        command = call("GET", headers["location"][0])[2]
    if command["status"] != "succeeded":
        problems.append(
            f"the command ended {command['status']} {command['result_code']}"
        )
    order = call("GET", f"/api/employee/orders/{order_id}")[2]
    if order["status"] != "confirmed":
        problems.append(f"the order is {order['status']}")
    status, _, repeat = call("POST", path, intent, key)
    if status != 200 or repeat["command_id"] != command["command_id"]:
        problems.append(f"a repeat answered {status} with {repeat}")
    status = call("POST", path, {**intent, "target_status": "cancelled"}, key)[0]
    if status != 409:
        problems.append(f"the same key with another intent answered {status}")
    direct = {"to_status": "preparing", "expected_status": "confirmed"}
    status = call("POST", f"/api/employee/orders/{order_id}/transition", direct)[0]
    if status != 403:
        problems.append(
            f"the direct transition answered {status} to a switched employee"
        )
    print(json.dumps(problems))


def send_command(order_id, phone, watch):
    """one new command on an order in created, watched for up to watch seconds; prints the answer and what it went through"""
    panel = Session()
    panel.send("GET", "/api/auth/csrf")

    def call(method, path, body=None, headers=None):
        sent = {
            "Content-Type": "application/json",
            "X-CSRFToken": panel.cookies.get("csrftoken", ""),
        }
        payload = json.dumps(body).encode() if body is not None else b""
        status, replied, content = panel.send(
            method, path, payload, {**sent, **(headers or {})}
        )
        return status, replied, json.loads(content) if content else None

    call(
        "POST",
        "/api/auth/login",
        {"phone": phone, "password": os.environ["SMOKE_EMPLOYEE_PASSWORD"]},
    )
    intent = {
        "expected_status": "created",
        "target_status": "confirmed",
        "reason": "smoke",
    }
    key = {"Idempotency-Key": f"smoke-{uuid.uuid4().hex}"}
    status, headers, command = call(
        "POST", f"/api/employee/orders/{order_id}/transition-commands", intent, key
    )
    seen = [command["status"]] if status in (200, 202) else []
    deadline = time.monotonic() + float(watch)
    while (
        seen and seen[-1] in ("pending", "dispatched") and time.monotonic() < deadline
    ):
        time.sleep(2)
        seen.append(call("GET", headers["location"][0])[2]["status"])
    print(
        json.dumps(
            {
                "status": status,
                "seen": sorted(set(seen)),
                "final": seen[-1] if seen else None,
            }
        )
    )


def prometheus(path, **params):
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    status, _, body = send("GET", path + query, host="prometheus", port=9090)
    if status != 200:
        raise SystemExit(f"prometheus answered {status} to {path}")
    return json.loads(body)["data"]


def value(promql, **params):
    return prometheus("/api/v1/query", query=promql, **params)["result"]


def sample_time():
    """prints an instant at which prometheus holds a sample of its own scrape"""
    try:
        found = value('up{job="prometheus"}')
    except (OSError, SystemExit):
        found = []
    if not found:
        raise SystemExit(1)
    print(found[0]["value"][0])


def has_sample(at):
    raise SystemExit(0 if value('up{job="prometheus"}', time=at) else 1)


def monitoring():
    problems = []
    jobs = set(
        re.findall(r"job_name: (\S+)", prometheus("/api/v1/status/config")["yaml"])
    )
    deadline = time.monotonic() + 120
    while True:
        targets = prometheus("/api/v1/targets")["activeTargets"]
        down = sorted(t["labels"]["job"] for t in targets if t["health"] != "up")
        seen = {t["labels"]["job"] for t in targets}
        if (not down and seen == jobs) or time.monotonic() > deadline:
            break
        time.sleep(3)
    if down or seen != jobs:
        problems.append(
            f"targets down {down}, configured {sorted(jobs)}, active {sorted(seen)}"
        )
    for promql, want in (
        ("prometheus_config_last_reload_successful", "1"),
        ("prometheus_tsdb_wal_writes_failed_total", "0"),
    ):
        result = value(promql)
        if not result or result[0]["value"][1] != want:
            problems.append(f"{promql} is {result}")
    appended = value("prometheus_tsdb_head_samples_appended_total")
    if not appended or float(appended[0]["value"][1]) <= 0:
        problems.append("the tsdb head took no samples")
    if not prometheus("/api/v1/rules")["groups"]:
        problems.append("no alert rules loaded")
    print(json.dumps(problems))


def health():
    try:
        status = send("GET", "/api/health")[0]
    except OSError:
        status = 0
    raise SystemExit(0 if status == 200 else 1)


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    commands = {
        "edge": edge,
        "upload": upload,
        "health": health,
        "monitoring": monitoring,
        "command_plane": command_plane,
        "send_command": send_command,
    }
    commands |= {"sample_time": sample_time, "has_sample": has_sample}
    commands[command](*args)
