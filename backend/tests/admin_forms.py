from html.parser import HTMLParser


class FormFields(HTMLParser):
    """what a browser submits for an admin change form: named inputs, checked boxes, selected options"""

    def __init__(self):
        super().__init__()
        self.data: dict[str, object] = {}
        self._select = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name")
        if tag == "input" and name and a.get("type") not in ("submit", "file", "button"):
            if a.get("type") == "checkbox":
                if "checked" in a:
                    self.data[name] = a.get("value") or "on"
            else:
                self.data[name] = a.get("value") or ""
        elif tag == "select" and name:
            self._select = name
        elif tag == "option" and self._select and "selected" in a:
            self.data.setdefault(self._select, a.get("value") or "")
        elif tag == "textarea" and name:
            self._textarea = name
            self.data[name] = ""

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self.data[self._textarea] = self.data[self._textarea] + data.lstrip("\n")


def rendered_form(client, url: str) -> dict:
    page = client.get(url)
    assert page.status_code == 200
    parser = FormFields()
    parser.feed(page.content.decode())
    parser.data.pop("csrfmiddlewaretoken", None)
    parser.data.pop("_continue", None)
    parser.data.pop("_addanother", None)
    return parser.data
