"""Rights-filtered, bounded visual retrieval from Wikimedia Commons."""

import json
import re
import struct
import unicodedata
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.media.providers import VisualAsset

_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_ALLOWED_IMAGE_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}
_ALLOWED_LICENSE_HOST = "creativecommons.org"
_ALLOWED_SOURCE_HOST = "commons.wikimedia.org"
_SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "before",
    "behind",
    "by",
    "close",
    "clear",
    "documentary",
    "distant",
    "during",
    "for",
    "from",
    "in",
    "into",
    "image",
    "of",
    "on",
    "over",
    "photo",
    "photograph",
    "realistic",
    "shot",
    "showing",
    "style",
    "the",
    "through",
    "to",
    "under",
    "with",
    "observing",
    "view",
}
_WIKIMEDIA_CONTACT_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_WIKIMEDIA_CONTACT_URL = re.compile(r"https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::443)?(?:/[^\s;)]*)?")
_ALLOWED_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class WikimediaVisualProviderError(RuntimeError):
    """A Wikimedia visual could not be retrieved with acceptable provenance."""


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainTextParser()
    parser.feed(unescape(value))
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def _metadata_value(metadata: dict, key: str) -> str:
    entry = metadata.get(key)
    if not isinstance(entry, dict):
        return ""
    return _plain_text(entry.get("value"))


def _license_details(metadata: dict) -> tuple[str, str, str] | None:
    short_name = " ".join(_metadata_value(metadata, "LicenseShortName").casefold().split())
    license_url = _metadata_value(metadata, "LicenseUrl")
    try:
        parsed_url = urlsplit(license_url)
        parsed_port = parsed_url.port
    except ValueError:
        return None
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != _ALLOWED_LICENSE_HOST
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_port not in {None, 443}
        or parsed_url.query
        or parsed_url.fragment
    ):
        return None

    license_path = parsed_url.path.rstrip("/")
    if short_name == "cc0 1.0" and license_path == "/publicdomain/zero/1.0":
        return "cc0", "CC0 1.0", license_url
    if short_name == "cc by 4.0" and license_path == "/licenses/by/4.0":
        return "cc_by", "CC BY 4.0", license_url
    return None


def _search_query(visual_direction: str, narration_segment: str) -> str:
    combined = f"{visual_direction} {narration_segment}"
    words = []
    for raw_word in combined.split():
        word = "".join(
            character
            for character in raw_word
            if unicodedata.category(character)[0] in {"L", "M", "N"}
        )
        if len(word) >= 2:
            words.append(word)
    query = " ".join(word[:48] for word in words[:24])
    if len(query) > 300:
        query = query[:300].rsplit(" ", 1)[0]
    if not query:
        raise WikimediaVisualProviderError("Scene description contains no searchable terms.")
    return query


def _is_allowed_image_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
        query = (
            parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            if parsed.query
            else []
        )
    except ValueError:
        return False
    allowed_tracking_values = {
        "utm_source": {"commons.wikimedia.org"},
        "utm_campaign": {"imageinfo"},
        "utm_content": {"thumbnail", "thumbnail_unscaled"},
    }
    query_is_safe = all(
        (key in {"width", "height"} and raw_value.isdigit() and 1 <= int(raw_value) <= 4096)
        or raw_value in allowed_tracking_values.get(key, set())
        for key, raw_value in query
    ) and len({key for key, _ in query}) == len(query)
    return (
        parsed.scheme == "https"
        and parsed.hostname in _ALLOWED_IMAGE_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed_port in {None, 443}
        and parsed.path.startswith("/wikipedia/commons/")
        and query_is_safe
        and not parsed.fragment
    )


def _is_allowed_source_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == _ALLOWED_SOURCE_HOST
        and parsed.username is None
        and parsed.password is None
        and parsed_port in {None, 443}
        and parsed.path.startswith("/wiki/File:")
        and not parsed.query
        and not parsed.fragment
    )


def _jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    """Read JPEG dimensions from a bounded SOF segment without decoding pixels."""
    if len(content) < 4 or not content.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 4 <= len(content):
        if content[offset] != 0xFF:
            return None
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset >= len(content):
            return None
        marker = content[offset]
        offset += 1
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(content):
            return None
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(content):
            return None
        if marker in _ALLOWED_JPEG_SOF_MARKERS:
            if segment_length < 8:
                return None
            component_count = content[offset + 7]
            if component_count < 1 or segment_length < 8 + 3 * component_count:
                return None
            height = int.from_bytes(content[offset + 3 : offset + 5], "big")
            width = int.from_bytes(content[offset + 5 : offset + 7], "big")
            return width, height
        if marker == 0xDA:
            return None
        offset += segment_length
    return None


def _image_dimensions(content: bytes, mime_type: str) -> tuple[int, int] | None:
    if mime_type == "image/png":
        if (
            len(content) < 24
            or not content.startswith(b"\x89PNG\r\n\x1a\n")
            or content[12:16] != b"IHDR"
        ):
            return None
        return struct.unpack(">II", content[16:24])
    if mime_type == "image/jpeg":
        return _jpeg_dimensions(content)
    return None


class WikimediaCommonsVisualProvider:
    """Fetch only CC0/CC BY 4.0 images with validated source/license metadata.

    ShareAlike, NonCommercial, NoDerivatives, public-domain-mark-only and
    unknown license declarations are excluded. Redirects and environment
    proxies are disabled; API and image response bodies are bounded.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 20.0,
        max_image_bytes: int = 20_000_000,
        max_api_response_bytes: int = 2_000_000,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        contact = user_agent.partition("(")[2].rpartition(")")[0].strip()
        has_email_contact = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", contact)
        has_https_contact = re.search(
            r"https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/|[;)\s]|$)", contact
        )
        if (
            not user_agent.startswith("AuroraStudio/")
            or len(user_agent) > 256
            or "\n" in user_agent
            or "\r" in user_agent
            or not (has_email_contact or has_https_contact)
        ):
            raise ValueError(
                "Wikimedia User-Agent must identify Aurora Studio and include contact information."
            )
        if timeout_seconds <= 0 or max_image_bytes <= 0 or max_api_response_bytes <= 0:
            raise ValueError("Wikimedia timeout and response-size limits must be positive.")
        self._max_image_bytes = max_image_bytes
        self._max_api_response_bytes = max_api_response_bytes
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            proxy=proxy_url,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, image/jpeg, image/png",
                "Accept-Encoding": "identity",
            },
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    async def _read_bounded_response(
        response: httpx.Response,
        *,
        max_bytes: int,
        expected_content_type: str,
    ) -> bytes:
        if response.status_code != 200:
            raise WikimediaVisualProviderError(
                f"Wikimedia returned HTTP status {response.status_code}."
            )
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime_type != expected_content_type:
            raise WikimediaVisualProviderError("Wikimedia response MIME type was unexpected.")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            if not content_length.isdecimal() or int(content_length) > max_bytes:
                raise WikimediaVisualProviderError(
                    "Wikimedia response exceeds the configured size limit."
                )

        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise WikimediaVisualProviderError(
                    "Wikimedia response exceeds the configured size limit."
                )
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            raise WikimediaVisualProviderError("Wikimedia returned an empty response.")
        return content

    async def _download_image(self, url: str, expected_mime: str) -> bytes:
        if not _is_allowed_image_url(url):
            raise WikimediaVisualProviderError("Wikimedia returned a non-allowlisted image URL.")
        try:
            async with self._client.stream("GET", url) as response:
                content = await self._read_bounded_response(
                    response,
                    max_bytes=self._max_image_bytes,
                    expected_content_type=expected_mime,
                )
        except httpx.HTTPError:
            raise WikimediaVisualProviderError("Wikimedia image request failed.") from None
        if _image_dimensions(content, expected_mime) is None:
            raise WikimediaVisualProviderError(
                "Wikimedia image failed format or dimension validation."
            )
        return content

    async def _search_pages(self, query: str) -> dict:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": 12,
            "prop": "imageinfo",
            "iiprop": "url|mime|size|dimensions|extmetadata",
            "iiurlwidth": 1024,
            "iiextmetadatalanguage": "en",
            "iiextmetadatafilter": (
                "LicenseShortName|LicenseUrl|Artist|Credit|AttributionRequired|Restrictions"
            ),
            "format": "json",
        }
        try:
            async with self._client.stream("GET", _COMMONS_API, params=params) as response:
                body = await self._read_bounded_response(
                    response,
                    max_bytes=self._max_api_response_bytes,
                    expected_content_type="application/json",
                )
        except httpx.HTTPError:
            raise WikimediaVisualProviderError("Wikimedia search request failed.") from None
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WikimediaVisualProviderError(
                "Wikimedia returned invalid search metadata."
            ) from None
        if not isinstance(payload, dict):
            raise WikimediaVisualProviderError("Wikimedia returned an invalid search response.")
        query_payload = payload.get("query")
        pages = query_payload.get("pages", {}) if isinstance(query_payload, dict) else {}
        if not isinstance(pages, dict):
            raise WikimediaVisualProviderError("Wikimedia returned an invalid page collection.")
        return pages

    async def generate(
        self,
        *,
        visual_direction: str,
        narration_segment: str,
        scene_index: int,
        visual_style: str = "",
        lighting: str = "cinematic",
    ) -> VisualAsset:
        if scene_index < 0:
            raise ValueError("Scene index cannot be negative.")
        query = _search_query(visual_direction, narration_segment)
        try:
            visual_query = _search_query(visual_direction, "")
        except WikimediaVisualProviderError:
            visual_query = query
        compact_terms = [
            term for term in visual_query.split() if term.casefold() not in _SEARCH_STOP_WORDS
        ][:4]
        compact_query = " ".join(compact_terms) or visual_query
        category_terms = (
            compact_query if len(compact_query) <= 240 else compact_query[:240].rsplit(" ", 1)[0]
        )
        category_query = f"{category_terms} incategory:CC-BY-4.0"
        pages: dict[str, object] = {}
        candidate_queries = list(dict.fromkeys((category_query, compact_query, query)))
        for candidate_query in candidate_queries:
            for title, page in (await self._search_pages(candidate_query)).items():
                pages[f"{candidate_query[:24]}:{title}"] = page

        def page_index(page: object) -> int:
            if not isinstance(page, dict):
                return 0
            try:
                return int(page.get("index", 0))
            except (TypeError, ValueError):
                return 0

        ordered_pages = sorted(pages.values(), key=page_index)
        for page in ordered_pages:
            if not isinstance(page, dict):
                continue
            imageinfo = page.get("imageinfo")
            if not isinstance(imageinfo, list) or not imageinfo:
                continue
            info = imageinfo[0]
            if not isinstance(info, dict):
                continue
            metadata = info.get("extmetadata")
            if not isinstance(metadata, dict):
                continue

            # Commons can serve JPEG thumbnails for a PDF's first page. Those
            # are document previews, not usable editorial photographs; reject
            # them so searches cannot silently choose screenshots of papers.
            original_mime = str(info.get("mime") or "").lower()
            if original_mime and original_mime not in {"image/jpeg", "image/png"}:
                continue

            license_details = _license_details(metadata)
            if license_details is None:
                continue
            rights_basis, license_id, license_uri = license_details
            restrictions = _metadata_value(metadata, "Restrictions").casefold()
            if restrictions and restrictions not in {
                "none",
                "no restrictions",
                "not applicable",
                "n/a",
            }:
                continue

            thumb_url = info.get("thumburl")
            mime_type = str(info.get("thumbmime") or "").lower()
            if mime_type not in {"image/jpeg", "image/png"}:
                image_path = urlsplit(thumb_url).path.lower() if isinstance(thumb_url, str) else ""
                mime_type = (
                    "image/png"
                    if image_path.endswith(".png")
                    else "image/jpeg"
                    if image_path.endswith((".jpg", ".jpeg"))
                    else str(info.get("mime") or "").lower()
                )
            if mime_type not in {"image/jpeg", "image/png"}:
                continue
            image_url = thumb_url
            source_uri = info.get("descriptionurl")
            if not _is_allowed_image_url(image_url) or not _is_allowed_source_url(source_uri):
                continue
            try:
                reported_width = int(info.get("thumbwidth") or info.get("width") or 0)
                reported_height = int(info.get("thumbheight") or info.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if (
                min(reported_width, reported_height) < 360
                or max(reported_width, reported_height) < 640
                or reported_width > 2160
                or reported_height > 3840
            ):
                continue

            artist = _metadata_value(metadata, "Artist")
            credit = _metadata_value(metadata, "Credit")
            attribution_required = _metadata_value(metadata, "AttributionRequired").casefold()
            attribution_name = artist or credit
            if rights_basis == "cc_by" and (
                attribution_required not in {"true", "yes", "1"} or not attribution_name
            ):
                continue

            title = _plain_text(page.get("title")) or "Wikimedia Commons image"
            attribution = (
                f"{attribution_name} — {title} — {source_uri} — "
                f"{license_id} ({license_uri}) — resized/cropped for video"
                if attribution_name
                else (
                    f"{title} — {source_uri} — {license_id} ({license_uri}) — "
                    "resized/cropped for video"
                )
            )
            try:
                content = await self._download_image(str(image_url), mime_type)
            except WikimediaVisualProviderError:
                continue
            actual_dimensions = _image_dimensions(content, mime_type)
            if actual_dimensions is None:
                continue
            width, height = actual_dimensions
            if (
                min(width, height) < 360
                or max(width, height) < 640
                or width > 2160
                or height > 3840
            ):
                continue

            return VisualAsset(
                content=content,
                mime_type=mime_type,
                width=width,
                height=height,
                provider_id="wikimedia_commons",
                rights_basis=rights_basis,
                attribution=attribution,
                source_uri=str(source_uri),
                license_id=license_id,
                license_uri=license_uri,
                contains_synthetic_media=False,
            )

        raise WikimediaVisualProviderError(
            "No Wikimedia result met the configured image and license requirements."
        )
