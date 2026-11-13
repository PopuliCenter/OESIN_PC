"""
Identifier normalization + OFFLINE phone enrichment.

Design boundary (PRD Non-Goals §4):
  * We NEVER derive GPS / home address from a phone number.
  * Enrichment here is purely metadata from the number's own structure
    (country, region, carrier band, line type, timezones) computed locally
    via Google's libphonenumber data. No network calls, no data brokers.

Online reputation/breach lookups are left as explicit, opt-in stubs so a
researcher can wire in an API key deliberately rather than by accident.
"""

import ipaddress
import re
from typing import Any, Optional
from urllib.parse import urlparse

import phonenumbers
from phonenumbers import carrier, geocoder, timezone as pn_timezone

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_.]{2,32}$")

NUMBER_TYPE_LABEL = {
    phonenumbers.PhoneNumberType.MOBILE: "mobile",
    phonenumbers.PhoneNumberType.FIXED_LINE: "fixed_line",
    phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_or_mobile",
    phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
    phonenumbers.PhoneNumberType.PREMIUM_RATE: "premium_rate",
    phonenumbers.PhoneNumberType.VOIP: "voip",
    phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "personal_number",
    phonenumbers.PhoneNumberType.PAGER: "pager",
    phonenumbers.PhoneNumberType.UAN: "uan",
    phonenumbers.PhoneNumberType.VOICEMAIL: "voicemail",
    phonenumbers.PhoneNumberType.UNKNOWN: "unknown",
}


def classify_identifier(raw: str) -> str:
    """Best-effort type detection for a free-text identifier."""
    v = raw.strip()
    if not v:
        return "unknown"
    # IP
    try:
        ipaddress.ip_address(v)
        return "ip"
    except ValueError:
        pass
    if v.startswith(("http://", "https://")):
        return "url"
    if EMAIL_RE.match(v):
        return "email"
    # phone: starts with + or is mostly digits
    digits = re.sub(r"[\s\-().]", "", v)
    if re.fullmatch(r"\+?\d{6,15}", digits):
        return "phone"
    if DOMAIN_RE.match(v):
        return "domain"
    if USERNAME_RE.match(v):
        return "username"
    return "unknown"


def normalize(id_type: str, raw: str) -> Optional[str]:
    v = raw.strip()
    if id_type == "email":
        return v.lower() if EMAIL_RE.match(v) else None
    if id_type == "domain":
        return v.lower() if DOMAIN_RE.match(v) else None
    if id_type == "url":
        host = urlparse(v).netloc.lower()
        return host or None
    if id_type == "ip":
        try:
            return str(ipaddress.ip_address(v))
        except ValueError:
            return None
    if id_type == "username":
        return v.lstrip("@").lower()
    if id_type == "phone":
        try:
            parsed = phonenumbers.parse(v, None)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            return None
    return None


def enrich_phone(raw: str, default_region: Optional[str] = None,
                 locale: str = "en") -> dict[str, Any]:
    """
    Offline enrichment of a phone number. Returns validity, E.164, country,
    geographic region name, carrier band, line type, and timezones.

    `default_region` (e.g. "ID") lets a user paste a local number like
    0812... without the +62 country code.
    """
    result: dict[str, Any] = {
        "input": raw,
        "valid": False,
        "source": "libphonenumber (offline)",
        "source_kind": "offline_local",
    }
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        result["error"] = f"parse_error: {exc._msg}"
        return result

    result["possible"] = phonenumbers.is_possible_number(parsed)
    result["valid"] = phonenumbers.is_valid_number(parsed)
    result["country_code"] = parsed.country_code
    result["national_number"] = parsed.national_number
    result["e164"] = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164)
    result["international"] = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    result["region"] = phonenumbers.region_code_for_number(parsed)
    result["location"] = geocoder.description_for_number(parsed, locale) or None
    result["carrier"] = carrier.name_for_number(parsed, locale) or None
    result["timezones"] = list(pn_timezone.time_zones_for_number(parsed))
    num_type = phonenumbers.number_type(parsed)
    result["line_type"] = NUMBER_TYPE_LABEL.get(num_type, "unknown")

    # Explicit reminder in the payload itself.
    result["note"] = (
        "Carrier/region are the number's registration metadata, NOT the "
        "device's live location. Real-time GPS is not derivable from a number."
    )
    return result


# --- Opt-in online enrichment stubs (disabled by default) --------------------
# Wire these in deliberately with your own API keys/ToS review. They stay off
# unless a key is provided, keeping the default build fully offline.

def reputation_lookup(e164: str, api_key: Optional[str] = None) -> dict[str, Any]:
    """Spam/scam reputation. Stub: returns 'not_configured' unless implemented."""
    if not api_key:
        return {"status": "not_configured",
                "hint": "Provide an API key and implement a vetted provider."}
    raise NotImplementedError("Plug a reputation provider here after ToS review.")


def breach_lookup(value: str, api_key: Optional[str] = None) -> dict[str, Any]:
    """Public breach exposure (e.g. HIBP-style). Stub, opt-in only."""
    if not api_key:
        return {"status": "not_configured",
                "hint": "Provide an API key and implement a vetted provider."}
    raise NotImplementedError("Plug a breach provider here after ToS review.")
