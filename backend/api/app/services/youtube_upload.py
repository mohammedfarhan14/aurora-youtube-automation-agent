"""Google YouTube Data API v3 resumable upload client.

All network requests use the explicitly configured allowlist proxy. Environment
proxy variables and HTTP redirects are deliberately ignored.
"""

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx

YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_THUMBNAIL_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
MAX_API_RESPONSE_BYTES = 1_000_000


class YouTubeUploadError(RuntimeError):
    """Base class for safe YouTube upload adapter failures."""


class YouTubeUploadTransientError(YouTubeUploadError):
    """A network or provider failure that may succeed on retry."""


class YouTubeUploadPermanentError(YouTubeUploadError):
    """A non-retryable provider rejection or invalid response."""


class YouTubeUploadSessionExpired(YouTubeUploadError):
    """The resumable session is gone; the job requires reconciliation."""


@dataclass(frozen=True, slots=True)
class UploadChunkResult:
    bytes_received: int
    video_id: str | None = None


@dataclass(frozen=True, slots=True)
class YouTubeVideoProcessingStatus:
    upload_status: str
    processing_status: str
    privacy_status: str
    published_at: str | None


def validate_upload_session_url(session_url: str) -> None:
    """Accept only HTTPS resumable session URLs on Google's exact upload host."""
    parsed = urlsplit(session_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise YouTubeUploadPermanentError(
            "Google returned an invalid resumable upload URL."
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.googleapis.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/upload/youtube/v3/videos")
        or not parse_qs(parsed.query).get("upload_id")
        or parsed.fragment
    ):
        raise YouTubeUploadPermanentError(
            "Google returned a resumable upload URL outside the approved endpoint."
        )


def _parse_json(response: httpx.Response) -> dict:
    if len(response.content) > MAX_API_RESPONSE_BYTES:
        raise YouTubeUploadPermanentError("YouTube API response exceeded the size limit.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise YouTubeUploadPermanentError("YouTube API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise YouTubeUploadPermanentError("YouTube API returned an invalid response shape.")
    return payload


def _raise_for_status(response: httpx.Response, *, upload_session: bool = False) -> None:
    if response.status_code in {200, 201, 308}:
        return
    if upload_session and response.status_code in {404, 410}:
        raise YouTubeUploadSessionExpired(
            "The resumable upload session expired or no longer exists."
        )
    if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
        raise YouTubeUploadTransientError(
            f"YouTube API temporarily rejected the request ({response.status_code})."
        )
    if response.status_code == 403:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        provider_errors = error.get("errors") if isinstance(error, dict) else None
        reasons = {
            item.get("reason")
            for item in provider_errors or []
            if isinstance(item, dict) and isinstance(item.get("reason"), str)
        }
        if "insufficientPermissions" in reasons:
            raise YouTubeUploadPermanentError(
                "YouTube accepted the video, but Google denied the visibility update because "
                "the channel OAuth grant lacks video-update permission. Reconnect the channel "
                "with publishing access, then resume this existing video; do not upload it again."
            )
        if "forbiddenPrivacySetting" in reasons:
            raise YouTubeUploadPermanentError(
                "YouTube accepted the video, but the channel does not allow the requested "
                "visibility setting. Change the visibility in YouTube Studio or select another "
                "visibility, then resume this existing video."
            )
    raise YouTubeUploadPermanentError(
        f"YouTube API rejected the request ({response.status_code})."
    )


def _received_bytes(response: httpx.Response) -> int:
    value = response.headers.get("range")
    if value is None:
        return 0
    prefix = "bytes=0-"
    if not value.startswith(prefix):
        raise YouTubeUploadPermanentError(
            "YouTube returned an invalid resumable upload range."
        )
    try:
        end = int(value[len(prefix):])
    except ValueError as exc:
        raise YouTubeUploadPermanentError(
            "YouTube returned an invalid resumable upload range."
        ) from exc
    if end < 0:
        raise YouTubeUploadPermanentError(
            "YouTube returned a negative resumable upload range."
        )
    return end + 1


class YouTubeResumableUploadClient:
    """Strict, proxy-only adapter for token refresh, chunk upload and processing polls."""

    def __init__(
        self,
        *,
        proxy_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: int = 120,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not proxy_url or not client_id or not client_secret:
            raise ValueError("YouTube upload proxy and OAuth credentials are required.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            proxy=proxy_url,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "AuroraStudio/1.0"},
        )

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Expose the same allowlisted-proxy client for OAuth token refresh."""
        return self._client

    async def refresh_access_token(self, refresh_token: str) -> str:
        try:
            response = await self._client.post(
                YOUTUBE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise YouTubeUploadTransientError(
                "Could not reach the Google OAuth token endpoint."
            ) from exc
        if response.status_code != 200:
            _raise_for_status(response)
        payload = _parse_json(response)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise YouTubeUploadPermanentError(
                "Google OAuth response did not contain an access token."
            )
        return access_token

    async def initiate_upload(
        self,
        *,
        access_token: str,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str,
        publish_at: str | None,
        mime_type: str,
        total_bytes: int,
        contains_synthetic_media: bool,
    ) -> str:
        if not isinstance(contains_synthetic_media, bool):
            raise ValueError("Synthetic-media disclosure must be an explicit boolean.")
        status = {
            "privacyStatus": privacy_status,
            "containsSyntheticMedia": contains_synthetic_media,
        }
        if publish_at is not None:
            if privacy_status != "private":
                raise YouTubeUploadPermanentError(
                    "Scheduled YouTube uploads must remain private."
                )
            status["publishAt"] = publish_at
        metadata = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
            },
            "status": status,
        }
        try:
            response = await self._client.post(
                YOUTUBE_UPLOAD_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": mime_type,
                    "X-Upload-Content-Length": str(total_bytes),
                },
                json=metadata,
            )
        except httpx.HTTPError as exc:
            raise YouTubeUploadTransientError(
                "Could not initiate the YouTube resumable upload."
            ) from exc
        if response.status_code not in {200, 201}:
            _raise_for_status(response)
        session_url = response.headers.get("location")
        if not isinstance(session_url, str):
            raise YouTubeUploadPermanentError(
                "Google did not return a resumable upload session URL."
            )
        validate_upload_session_url(session_url)
        return session_url

    async def upload_chunk(
        self,
        *,
        access_token: str,
        session_url: str,
        content: bytes,
        start: int,
        total_bytes: int,
    ) -> UploadChunkResult:
        validate_upload_session_url(session_url)
        if not content or start < 0 or total_bytes <= start:
            raise ValueError("YouTube upload chunk bounds are invalid.")
        end = start + len(content) - 1
        if end >= total_bytes:
            raise ValueError("YouTube upload chunk exceeds the declared file size.")
        try:
            response = await self._client.put(
                session_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(content)),
                    "Content-Range": f"bytes {start}-{end}/{total_bytes}",
                },
                content=content,
            )
        except httpx.HTTPError as exc:
            raise YouTubeUploadTransientError(
                "The YouTube resumable upload chunk request failed."
            ) from exc
        _raise_for_status(response, upload_session=True)
        if response.status_code == 308:
            received = _received_bytes(response)
            if received > total_bytes:
                raise YouTubeUploadPermanentError(
                    "YouTube acknowledged more bytes than the source artifact contains."
                )
            return UploadChunkResult(bytes_received=received)
        payload = _parse_json(response)
        video_id = payload.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise YouTubeUploadPermanentError(
                "YouTube completed the upload without returning a video ID."
            )
        return UploadChunkResult(bytes_received=total_bytes, video_id=video_id)

    async def query_upload_status(
        self,
        *,
        access_token: str,
        session_url: str,
        total_bytes: int,
    ) -> UploadChunkResult:
        validate_upload_session_url(session_url)
        try:
            response = await self._client.put(
                session_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{total_bytes}",
                },
                content=b"",
            )
        except httpx.HTTPError as exc:
            raise YouTubeUploadTransientError(
                "Could not reconcile the YouTube resumable upload session."
            ) from exc
        _raise_for_status(response, upload_session=True)
        if response.status_code == 308:
            received = _received_bytes(response)
            if received > total_bytes:
                raise YouTubeUploadPermanentError(
                    "YouTube session offset exceeds the source artifact size."
                )
            return UploadChunkResult(bytes_received=received)
        payload = _parse_json(response)
        video_id = payload.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise YouTubeUploadPermanentError(
                "YouTube completed the upload without returning a video ID."
            )
        return UploadChunkResult(bytes_received=total_bytes, video_id=video_id)

    async def upload_thumbnail(
        self,
        *,
        access_token: str,
        video_id: str,
        content: bytes,
        mime_type: str,
        processing_status: str,
    ) -> dict:
        """Upload a custom Shorts thumbnail after YouTube processing succeeds."""
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("YouTube access token is required.")
        if not isinstance(video_id, str) or not video_id.strip() or len(video_id) > 128:
            raise ValueError("YouTube video ID is invalid.")
        if processing_status != "succeeded":
            raise ValueError("A Shorts thumbnail can only be set after video processing succeeds.")
        if (
            mime_type not in {"image/jpeg", "image/png"}
            or not isinstance(content, bytes)
            or not content
            or len(content) > 50_000_000
        ):
            raise ValueError("Thumbnail image must be a non-empty JPEG or PNG under 50 MB.")
        try:
            response = await self._client.post(
                YOUTUBE_THUMBNAIL_UPLOAD_URL,
                params={"videoId": video_id, "uploadType": "media"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": mime_type,
                },
                content=content,
            )
        except httpx.HTTPError as exc:
            raise YouTubeUploadTransientError(
                "Could not upload the custom YouTube thumbnail."
            ) from exc
        if response.status_code != 200:
            _raise_for_status(response)
        payload = _parse_json(response)
        items = payload.get("items")
        if (
            payload.get("kind") != "youtube#thumbnailSetResponse"
            or not isinstance(items, list)
            or not items
            or not isinstance(items[0], dict)
        ):
            raise YouTubeUploadPermanentError(
                "YouTube thumbnail upload returned an invalid response."
            )
        return payload

    async def update_video_privacy_status(
        self,
        *,
        access_token: str,
        video_id: str,
        privacy_status: str,
    ) -> str:
        """Apply the owner's approved visibility only after YouTube processing succeeds."""
        if privacy_status not in {"private", "unlisted", "public"}:
            raise ValueError("YouTube privacy status is invalid.")
        try:
            response = await self._client.put(
                YOUTUBE_VIDEOS_URL,
                params={"part": "status"},
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "id": video_id,
                    "status": {"privacyStatus": privacy_status},
                },
            )
        except httpx.HTTPError as exc:
            raise YouTubeUploadTransientError(
                "Could not apply the approved YouTube video visibility."
            ) from exc
        if response.status_code != 200:
            _raise_for_status(response)
        payload = _parse_json(response)
        if payload.get("id") != video_id:
            raise YouTubeUploadPermanentError(
                "YouTube visibility update returned a different video resource."
            )
        status_data = payload.get("status")
        if not isinstance(status_data, dict):
            raise YouTubeUploadTransientError(
                "YouTube visibility update did not return video status."
            )
        observed_privacy = status_data.get("privacyStatus")
        if observed_privacy not in {"private", "unlisted", "public"}:
            raise YouTubeUploadTransientError(
                "YouTube visibility update returned an unknown privacy status."
            )
        return observed_privacy

    async def get_video_processing_status(
        self,
        *,
        access_token: str,
        video_id: str,
    ) -> YouTubeVideoProcessingStatus:
        try:
            response = await self._client.get(
                YOUTUBE_VIDEOS_URL,
                params={"part": "snippet,status,processingDetails", "id": video_id},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise YouTubeUploadTransientError(
                "Could not retrieve YouTube video processing status."
            ) from exc
        if response.status_code != 200:
            _raise_for_status(response)
        payload = _parse_json(response)
        items = payload.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise YouTubeUploadPermanentError(
                "YouTube did not return the uploaded video resource."
            )
        item = items[0]
        status_data = item.get("status")
        processing = item.get("processingDetails")
        snippet = item.get("snippet")
        if not isinstance(status_data, dict) or not isinstance(processing, dict):
            raise YouTubeUploadTransientError(
                "YouTube has not exposed processing status for the uploaded video yet."
            )
        return YouTubeVideoProcessingStatus(
            upload_status=str(status_data.get("uploadStatus", "unknown")),
            processing_status=str(processing.get("processingStatus", "unknown")),
            privacy_status=str(status_data.get("privacyStatus", "private")),
            published_at=(
                snippet.get("publishedAt")
                if isinstance(snippet, dict)
                and isinstance(snippet.get("publishedAt"), str)
                else None
            ),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
