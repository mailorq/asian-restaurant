"""an sms provider for the tests: what would have gone out, in order"""

SENT: list[tuple[str, str]] = []


class MemorySender:
    available = True

    def send(self, phone: str, text: str) -> None:
        SENT.append((phone, text))


class FailingSender:
    # a provider whose call fails after the message may already have left
    available = True

    def send(self, phone: str, text: str) -> None:
        SENT.append((phone, text))
        raise TimeoutError("the provider did not answer")
