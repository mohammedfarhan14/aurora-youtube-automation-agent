import asyncio
import json

import httpx
import pytest

from app.services.youtube_upload import (
    YouTubeResumableUploadClient,
    YouTubeUploadPermanentError,
    YouTubeUploadTransientError,
)

SESSION_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=opaque-session"
)


def test_youtube_resumable_client_refreshes_token_and_initiates_upload():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-secret"})
        return httpx.Response(200, headers={"Location": SESSION_URL})

    async def run() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )
        client = YouTubeResumableUploadClient(
            proxy_url="http://youtube-egress-proxy:3128",
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        try:
            token = await client.refresh_access_token("refresh-secret")
            assert token == "access-secret"
            session_url = await client.initiate_upload(
                access_token=token,
                title="Research short",
                description="Evidence-backed description with credits.",
                tags=["science", "energy"],
                privacy_status="private",
                publish_at=None,
                mime_type="video/mp4",
                total_bytes=1024,
                contains_synthetic_media=True,
            )
            assert session_url == SESSION_URL
        finally:
            await http_client.aclose()

    asyncio.run(run())
    assert len(requests) == 2
    assert requests[0].url == "https://oauth2.googleapis.com/token"
    assert requests[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert requests[1].url.host == "www.googleapis.com"
    assert requests[1].headers["x-upload-content-length"] == "1024"
    metadata = json.loads(requests[1].content)
    assert metadata["snippet"]["title"] == "Research short"
    assert metadata["status"]["privacyStatus"] == "private"
    assert metadata["status"]["containsSyntheticMedia"] is True


def test_youtube_resumable_client_uploads_chunks_and_reconciles_ranges():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert request.headers["content-range"] == "bytes 0-3/8"
            return httpx.Response(308, headers={"Range": "bytes=0-3"})
        if calls == 2:
            assert request.headers["content-range"] == "bytes 4-7/8"
            return httpx.Response(201, json={"id": "youtube-video-id"})
        if calls == 3:
            assert request.headers["content-range"] == "bytes */8"
            return httpx.Response(308, headers={"Range": "bytes=0-5"})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "status": {
                            "uploadStatus": "processed",
                            "privacyStatus": "public",
                        },
                        "processingDetails": {"processingStatus": "succeeded"},
                        "snippet": {"publishedAt": "2026-11-13T02:00:00Z"},
                    }
                ]
            },
        )

    async def run() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )
        client = YouTubeResumableUploadClient(
            proxy_url="http://youtube-egress-proxy:3128",
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        try:
            first = await client.upload_chunk(
                access_token="access",
                session_url=SESSION_URL,
                content=b"1234",
                start=0,
                total_bytes=8,
            )
            assert first.bytes_received == 4
            assert first.video_id is None
            second = await client.upload_chunk(
                access_token="access",
                session_url=SESSION_URL,
                content=b"5678",
                start=4,
                total_bytes=8,
            )
            assert second.bytes_received == 8
            assert second.video_id == "youtube-video-id"
            reconciled = await client.query_upload_status(
                access_token="access",
                session_url=SESSION_URL,
                total_bytes=8,
            )
            assert reconciled.bytes_received == 6
            status = await client.get_video_processing_status(
                access_token="access",
                video_id="youtube-video-id",
            )
            assert status.upload_status == "processed"
            assert status.processing_status == "succeeded"
            assert status.privacy_status == "public"
            assert status.published_at == "2026-11-13T02:00:00Z"
        finally:
            await http_client.aclose()

    asyncio.run(run())
    assert calls == 4


def test_youtube_resumable_client_never_follows_redirects_or_accepts_other_hosts():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.example/steal"},
        )

    async def run() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )
        client = YouTubeResumableUploadClient(
            proxy_url="http://youtube-egress-proxy:3128",
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        try:
            with pytest.raises(YouTubeUploadPermanentError, match="302"):
                await client.refresh_access_token("refresh-secret")
        finally:
            await http_client.aclose()

    asyncio.run(run())
    assert calls == 1


def test_youtube_resumable_client_classifies_transient_and_permanent_errors():
    async def run(status_code: int, expected: type[Exception]) -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status_code, json={"error": "redacted"})
            ),
            trust_env=False,
            follow_redirects=False,
        )
        client = YouTubeResumableUploadClient(
            proxy_url="http://youtube-egress-proxy:3128",
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        try:
            with pytest.raises(expected):
                await client.refresh_access_token("refresh-secret")
        finally:
            await http_client.aclose()

    asyncio.run(run(503, YouTubeUploadTransientError))
    asyncio.run(run(403, YouTubeUploadPermanentError))


def test_youtube_resumable_client_rejects_untrusted_session_location():
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={
                    "Location": (
                        "https://attacker.example/upload/youtube/v3/videos"
                        "?upload_id=stolen"
                    )
                },
            )
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = YouTubeResumableUploadClient(
        proxy_url="http://youtube-egress-proxy:3128",
        client_id="client-id",
        client_secret="client-secret",
        http_client=http_client,
    )

    async def run() -> None:
        try:
            with pytest.raises(YouTubeUploadPermanentError, match="outside the approved"):
                await client.initiate_upload(
                    access_token="access",
                    title="Safe title",
                    description="Attribution",
                    tags=[],
                    privacy_status="private",
                    publish_at=None,
                    mime_type="video/mp4",
                    total_bytes=1024,
                    contains_synthetic_media=False,
                )
        finally:
            await http_client.aclose()

    asyncio.run(run())


def test_youtube_resumable_client_marks_expired_sessions_for_reconciliation():
    from app.services.youtube_upload import YouTubeUploadSessionExpired

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(410)
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = YouTubeResumableUploadClient(
        proxy_url="http://youtube-egress-proxy:3128",
        client_id="client-id",
        client_secret="client-secret",
        http_client=http_client,
    )

    async def run() -> None:
        try:
            with pytest.raises(YouTubeUploadSessionExpired):
                await client.query_upload_status(
                    access_token="access",
                    session_url=SESSION_URL,
                    total_bytes=1024,
                )
        finally:
            await http_client.aclose()

    asyncio.run(run())


def test_youtube_resumable_client_applies_approved_visibility_after_processing():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "PUT"
        assert request.url.path == "/youtube/v3/videos"
        assert request.url.params["part"] == "status"
        payload = json.loads(request.content)
        assert payload == {
            "id": "youtube-video-id",
            "status": {"privacyStatus": "public"},
        }
        return httpx.Response(
            200,
            json={
                "id": "youtube-video-id",
                "status": {"privacyStatus": "public"},
            },
        )

    async def run() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )
        client = YouTubeResumableUploadClient(
            proxy_url="http://youtube-egress-proxy:3128",
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        try:
            observed = await client.update_video_privacy_status(
                access_token="access-secret",
                video_id="youtube-video-id",
                privacy_status="public",
            )
            assert observed == "public"
        finally:
            await http_client.aclose()

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer access-secret"


def test_youtube_resumable_client_rejects_visibility_update_for_unknown_privacy():
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        ),
        trust_env=False,
        follow_redirects=False,
    )
    client = YouTubeResumableUploadClient(
        proxy_url="http://youtube-egress-proxy:3128",
        client_id="client-id",
        client_secret="client-secret",
        http_client=http_client,
    )

    async def run() -> None:
        try:
            with pytest.raises(ValueError, match="privacy status is invalid"):
                await client.update_video_privacy_status(
                    access_token="access-secret",
                    video_id="youtube-video-id",
                    privacy_status="scheduled",
                )
        finally:
            await http_client.aclose()

    asyncio.run(run())


def test_upload_client_exposes_its_proxy_bound_http_client():
    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        ))
        upload_client = YouTubeResumableUploadClient(
            proxy_url="http://127.0.0.1:3128",
            client_id="test-client",
            client_secret="test-secret",
            http_client=client,
        )
        try:
            assert upload_client.http_client is client
        finally:
            await client.aclose()

    asyncio.run(run())


def test_youtube_thumbnail_upload_uses_mock_transport_after_processing():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "kind": "youtube#thumbnailSetResponse",
                "items": [{"default": {"url": "https://i.ytimg.com/vi/test/default.jpg"}}],
            },
        )

    async def run() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        )
        client = YouTubeResumableUploadClient(
            proxy_url="http://youtube-egress-proxy:3128",
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        try:
            result = await client.upload_thumbnail(
                access_token="mock-access-token",
                video_id="youtube-video-id",
                content=b"mock-jpeg-bytes",
                mime_type="image/jpeg",
                processing_status="succeeded",
            )
            assert result["kind"] == "youtube#thumbnailSetResponse"
        finally:
            await http_client.aclose()

    asyncio.run(run())
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.host == "www.googleapis.com"
    assert request.url.path == "/upload/youtube/v3/thumbnails/set"
    assert request.url.params["videoId"] == "youtube-video-id"
    assert request.url.params["uploadType"] == "media"
    assert request.headers["authorization"] == "Bearer mock-access-token"
    assert request.headers["content-type"] == "image/jpeg"
    assert request.content == b"mock-jpeg-bytes"


def test_youtube_thumbnail_upload_refuses_before_processing_or_invalid_media():
    async def run() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            trust_env=False,
            follow_redirects=False,
        )
        client = YouTubeResumableUploadClient(
            proxy_url="http://youtube-egress-proxy:3128",
            client_id="client-id",
            client_secret="client-secret",
            http_client=http_client,
        )
        try:
            with pytest.raises(ValueError, match="processing succeeds"):
                await client.upload_thumbnail(
                    access_token="token",
                    video_id="youtube-video-id",
                    content=b"image",
                    mime_type="image/jpeg",
                    processing_status="processing",
                )
            with pytest.raises(ValueError, match="JPEG or PNG"):
                await client.upload_thumbnail(
                    access_token="token",
                    video_id="youtube-video-id",
                    content=b"image",
                    mime_type="text/plain",
                    processing_status="succeeded",
                )
        finally:
            await http_client.aclose()

    asyncio.run(run())


@pytest.mark.anyio
async def test_upload_thumbnail_rejects_images_over_youtube_size_limit():
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )
    client = YouTubeResumableUploadClient(
        proxy_url="http://youtube-egress-proxy:3128",
        client_id="client-id",
        client_secret="client-secret",
        http_client=http_client,
    )
    try:
        with pytest.raises(ValueError, match="50 MB"):
            await client.upload_thumbnail(
                access_token="mock-token",
                video_id="video-123",
                content=b"x" * 50_000_001,
                mime_type="image/jpeg",
                processing_status="succeeded",
            )
    finally:
        await http_client.aclose()
