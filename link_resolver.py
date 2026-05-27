import logging
import re

import httpx
from telegram import Message
from telegram.constants import MessageEntityType

from config import REDIRECT_TIMEOUT_SEC, USER_AGENT

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.I)

ASIN_PATTERNS = [
    r"/dp/([A-Z0-9]{10})",
    r"/gp/product/([A-Z0-9]{10})",
    r"/product/([A-Z0-9]{10})",
]

ASIN_ONLY_PATTERN = re.compile(r"\b([A-Z0-9]{10})\b", re.I)

_http_client: httpx.AsyncClient | None = None


async def init_http_client() -> None:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(REDIRECT_TIMEOUT_SEC, connect=5.0),
            headers={"User-Agent": USER_AGENT},
        )
        logger.info("HTTP redirect client ready")


async def close_http_client() -> None:
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None
        logger.info("HTTP redirect client closed")


def get_message_text(msg: Message) -> str:
    return getattr(msg, "text", None) or getattr(msg, "caption", None) or ""


def _normalize_url(url: str) -> str:
    return url.strip().rstrip(".,)>]")


def extract_all_urls_from_text(text: str) -> list[str]:
    """Extract all URLs from text using findall, deduplicated in order."""
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_PATTERN.findall(text.strip()):
        url = _normalize_url(match)
        key = url.lower()
        if key and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def extract_all_urls_from_message(msg: Message) -> list[str]:
    """All URLs from plain text, caption, and Telegram entities."""
    precomputed = getattr(msg, "urls", None)
    if precomputed is not None:
        return list(precomputed)

    text = get_message_text(msg)
    seen: set[str] = {u.lower() for u in extract_all_urls_from_text(text)}
    urls: list[str] = extract_all_urls_from_text(text)

    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == MessageEntityType.URL and text:
            url = _normalize_url(text[ent.offset : ent.offset + ent.length])
            key = url.lower()
            if key and key not in seen:
                seen.add(key)
                urls.append(url)
        elif ent.type == MessageEntityType.TEXT_LINK and ent.url:
            url = _normalize_url(ent.url)
            key = url.lower()
            if key and key not in seen:
                seen.add(key)
                urls.append(url)

    return urls


def extract_url_from_message(msg: Message) -> str | None:
    """First URL only (backward compatible)."""
    urls = extract_all_urls_from_message(msg)
    return urls[0] if urls else None


def extract_asin(url: str) -> str | None:
    for pattern in ASIN_PATTERNS:
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1).upper()
    return None


def is_standalone_asin(text: str) -> str | None:
    """Return ASIN if text is a single 10-char product id."""
    token = text.strip().upper()
    if ASIN_ONLY_PATTERN.fullmatch(token):
        return token
    return None


def extract_manual_inputs(text: str) -> list[str]:
    """
    Extract URLs and standalone ASINs from admin manual input text.
    Returns URLs and bare ASIN strings, deduplicated in order.
    """
    if not text:
        return []
    urls = extract_all_urls_from_text(text)
    seen_asins: set[str] = set()
    for url in urls:
        asin = extract_asin(url)
        if asin:
            seen_asins.add(asin)

    remaining = text
    for url in urls:
        remaining = remaining.replace(url, " ")

    inputs: list[str] = []
    seen_keys: set[str] = set()

    for url in urls:
        key = url.lower()
        if key not in seen_keys:
            seen_keys.add(key)
            inputs.append(url)

    for match in ASIN_ONLY_PATTERN.finditer(remaining):
        asin = match.group(1).upper()
        if asin in seen_asins:
            continue
        key = f"asin:{asin}"
        if key not in seen_keys:
            seen_keys.add(key)
            seen_asins.add(asin)
            inputs.append(asin)

    return inputs


def build_clean_url(asin: str, domain: str) -> str:
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    return f"https://{domain}/dp/{asin}"


async def resolve_redirect(url: str) -> str:
    """Fast HTTP redirect resolution (no Playwright)."""
    if _http_client is None:
        await init_http_client()

    assert _http_client is not None

    try:
        response = await _http_client.head(url)
        return str(response.url)
    except httpx.HTTPError:
        response = await _http_client.get(url)
        return str(response.url)
