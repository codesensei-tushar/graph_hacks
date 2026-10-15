"""Port of company-brain `brain/company-domain.ts`. Shared by install bootstrap; no imports."""

import re

FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "fastmail.com",
        "zoho.com",
        "yandex.com",
        "mail.com",
        "qq.com",
        "163.com",
        "naver.com",
    }
)


def company_domain(domain: str | None) -> str | None:
    """A real company domain, or None for empty and free-mail domains."""
    clean = (domain or "").strip().lower()
    if not clean or clean in FREE_EMAIL_DOMAINS:
        return None
    return clean


def normalize_company_domain(value: str) -> str | None:
    """User-supplied text ("https://www.example.com/about") down to a bare domain."""
    host = value.strip().lower()
    host = re.sub(r"^https?://", "", host)
    host = re.sub(r"^www\.", "", host)
    host = re.sub(r"[/?#].*$", "", host)
    labels = host.split(".")
    if len(labels) < 2:
        return None
    if not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", label) for label in labels):
        return None
    if not re.fullmatch(r"[a-z]{2,}", labels[-1]):
        return None
    return company_domain(host)
