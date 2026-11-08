"""
http load load_smoke.sh sends from where the tls ingress stands. the ingress forwards each client under its own address,
so every simulated client here carries one in X-Forwarded-For and gets the rate-limit bucket a real one would
"""

import json
import os
import socket
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

HOST = "load.invalid"
ADDRESS = "ул. Нагрузочная, 1"


def send(method, path, body=b"", headers=None):
    """http/1.0 over a raw socket, timed from the connect to the last byte"""
    lines = [f"{method} {path} HTTP/1.0", f"Host: {HOST}", "X-Forwarded-Proto: https"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    started = time.perf_counter()
    with socket.create_connection(("frontend", 8080), timeout=60) as conn:
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        raw = b""
        while chunk := conn.recv(65536):
            raw += chunk
    took = time.perf_counter() - started
    head, _, content = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    replied = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        replied.setdefault(name.strip().lower(), []).append(value.strip())
    return int(status_line.split()[1]), replied, content, took


class Client:
    """one visitor: its cookies and the address the ingress would forward for it"""

    def __init__(self, address):
        self.address = address
        self.cookies = {}

    def send(self, method, path, payload=None):
        headers = {
            "X-Forwarded-For": self.address,
            "Origin": f"https://{HOST}",
            "Referer": f"https://{HOST}/",
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }
        body = b""
        if payload is not None:
            body = json.dumps(payload).encode()
            headers |= {
                "Content-Type": "application/json",
                "X-CSRFToken": self.cookies.get("csrftoken", ""),
            }
        status, replied, content, took = send(method, path, body, headers)
        for header in replied.get("set-cookie", []):
            name, _, value = header.split(";", 1)[0].partition("=")
            self.cookies[name] = value
        return status, replied, content, took

    def sign_in(self, login, password):
        self.send("GET", "/api/auth/csrf")
        status = self.send(
            "POST", "/api/auth/login", {"login": login, "password": password}
        )[0]
        if status != 200:
            raise SystemExit(
                json.dumps({"problems": [f"{login} could not sign in: {status}"]})
            )


def address(n):
    # 198.18.0.0/15 is set aside for benchmarks, so no simulated client is anyone real
    return f"198.18.{n // 250}.{n % 250 + 1}"


def summary(statuses, times, wall):
    ordered = sorted(times)

    def at(share):
        return round(
            ordered[min(len(ordered) - 1, int(share * len(ordered)))] * 1000, 1
        )

    histogram = {}
    for status in statuses:
        histogram[str(status)] = histogram.get(str(status), 0) + 1
    return {
        "requests": len(statuses),
        "statuses": histogram,
        "ms": {
            "p50": at(0.5),
            "p95": at(0.95),
            "p99": at(0.99),
            "max": round(ordered[-1] * 1000, 1),
        },
        "mean_ms": round(statistics.fmean(times) * 1000, 1),
        "per_second": round(len(statuses) / wall, 1),
    }


def menu(concurrency, total):
    """the catalog read by many clients at once, each under its own address"""
    statuses, times, lock = [], [], threading.Lock()

    def one(n):
        status, _, _, took = Client(address(n % concurrency)).send(
            "GET", "/api/menu/products"
        )
        with lock:
            statuses.append(status)
            times.append(took)

    started = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as pool:
        list(pool.map(one, range(total)))
    result = summary(statuses, times, time.perf_counter() - started)
    result["conditions"] = {"clients": concurrency, "requests": total}
    result["problems"] = [
        f"the catalog answered {s}" for s in sorted(set(statuses)) if s != 200
    ]
    return result


def checkout(product_id, logins, password):
    """every buyer puts one unit in the cart, then all of them check out at the same moment"""
    clients = []
    for n, login in enumerate(logins):
        client = Client(address(1000 + n))
        client.sign_in(login, password)
        status = client.send(
            "POST", "/api/cart/items", {"product_id": product_id, "quantity": 1}
        )[0]
        if status != 200:
            raise SystemExit(
                json.dumps({"problems": [f"{login} could not fill the cart: {status}"]})
            )
        clients.append(client)
    gate = threading.Barrier(len(clients))
    answers, lock = [], threading.Lock()

    def buy(client):
        gate.wait()
        payload = {
            "address": ADDRESS,
            "payment_method": "cash",
            "idempotency_key": uuid.uuid4().hex,
        }
        status, _, content, took = client.send("POST", "/api/orders/checkout", payload)
        code = (
            json.loads(content).get("code")
            if status != 200 and content.startswith(b"{")
            else None
        )
        with lock:
            answers.append((status, code, took))

    started = time.perf_counter()
    with ThreadPoolExecutor(len(clients)) as pool:
        list(pool.map(buy, clients))
    result = summary(
        [a[0] for a in answers], [a[2] for a in answers], time.perf_counter() - started
    )
    result["conditions"] = {"buyers": len(clients), "units_each": 1}
    result["ordered"] = sum(1 for status, _, _ in answers if status == 200)
    result["problems"] = [
        f"a checkout answered {status} {code}"
        for status, code, _ in answers
        if not (status == 200 or (status == 409 and code == "cart_changed"))
    ]
    return result


def limiter(product_id, login, password):
    """one client past the checkout and sign in limits: the request over each limit is refused with 429"""
    client = Client(address(2000))
    client.sign_in(login, password)
    client.send("POST", "/api/cart/items", {"product_id": product_id, "quantity": 1})
    checkouts = []
    for _ in range(12):
        payload = {
            "address": ADDRESS,
            "payment_method": "cash",
            "idempotency_key": uuid.uuid4().hex,
        }
        status, replied, _, _ = client.send("POST", "/api/orders/checkout", payload)
        checkouts.append((status, replied.get("retry-after", [""])[0]))
    stranger = Client(address(2001))
    stranger.send("GET", "/api/auth/csrf")
    logins = [
        stranger.send(
            "POST", "/api/auth/login", {"login": login, "password": "wrong-on-purpose"}
        )[0]
        for _ in range(6)
    ]
    problems = []
    if [s for s, _ in checkouts[:10] if s == 429] or [
        s for s, _ in checkouts[10:] if s != 429
    ]:
        problems.append(
            f"checkout limit 10 per minute did not hold: {[s for s, _ in checkouts]}"
        )
    if any(after != "60" for s, after in checkouts[10:]):
        problems.append(
            f"a refused checkout did not say when to retry: {checkouts[10:]}"
        )
    if 429 in logins[:5] or logins[5] != 429:
        problems.append(f"sign in limit 5 per 10 seconds did not hold: {logins}")
    return {
        "checkouts": [s for s, _ in checkouts],
        "sign_ins": logins,
        "conditions": {
            "checkout": "10 per 60 s per client",
            "sign_in": "5 per 10 s per client",
        },
        "problems": problems,
    }


if __name__ == "__main__":
    what, args = sys.argv[1], json.loads(sys.argv[2])
    password = os.environ["LOAD_PASSWORD"]
    if what == "menu":
        out = menu(args["clients"], args["requests"])
    elif what == "checkout":
        out = checkout(args["product_id"], args["logins"], password)
    elif what == "limits":
        out = limiter(args["product_id"], args["login"], password)
    else:
        raise SystemExit(f"unknown measurement {what}")
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1 if out["problems"] else 0)
