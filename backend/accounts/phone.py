import phonenumbers

# a number typed without a country code is read as ukrainian
DEFAULT_REGION = "UA"


def to_e164(raw: str) -> str | None:
    try:
        parsed = phonenumbers.parse(raw, DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
