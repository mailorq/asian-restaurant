"""an sms provider for the tests: what would have gone out, in order"""

SENT: list[tuple[str, str]] = []


class MemorySender:
    available = True

    def send(self, phone: str, text: str) -> None:
        SENT.append((phone, text))
