import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

from app.media.s3_storage import S3CompatibleMediaStorage
from app.media.storage import MediaObjectTooLarge, MediaStorageError, UnsafeMediaStorageKey


def verify_sigv4_signature(request, *, secret_access_key: str) -> bool:
    authorization = request.headers["authorization"]
    credential = authorization.split("Credential=", 1)[1].split(",", 1)[0]
    access_key, scope = credential.split("/", 1)
    signed_headers = authorization.split("SignedHeaders=", 1)[1].split(",", 1)[0]
    signature = authorization.split("Signature=", 1)[1]
    canonical_headers = "".join(
        f"{name}:{request.headers[name].strip()}\n"
        for name in signed_headers.split(";")
    )
    canonical_request = "\n".join(
        (
            request.method,
            request.url.raw_path.decode("ascii").split("?", 1)[0],
            request.url.query.decode("ascii"),
            canonical_headers,
            signed_headers,
            request.headers["x-amz-content-sha256"],
        )
    )
    amz_date = request.headers["x-amz-date"]
    date_stamp = amz_date[:8]
    region = scope.split("/")[1]
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    date_key = hmac.new(
        ("AWS4" + secret_access_key).encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    signing_key = hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
    expected = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return access_key == "test-access" and hmac.compare_digest(signature, expected)


@pytest.mark.anyio
async def test_s3_storage_signs_and_round_trips_private_tenant_scoped_objects():
    content = b"synthetic private media bytes"
    requests = []

    async def handle(request):
        requests.append(request)
        if request.method == "PUT":
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(200, content=content)
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected method: {request.method}")

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://account.r2.cloudflarestorage.com",
        bucket="aurora-media",
        region="auto",
        access_key_id="test-access",
        secret_access_key="test-secret",
        session_token="test-session",
        max_object_bytes=1024,
        transport=httpx.MockTransport(handle),
    )
    organization_id = uuid4()
    artifact_id = uuid4()

    stored = await storage.put(
        organization_id=organization_id,
        artifact_id=artifact_id,
        extension="mp4",
        mime_type="video/mp4",
        content=content,
    )
    downloaded = await storage.get(stored.storage_key)
    await storage.delete(stored.storage_key)

    assert stored.storage_key == f"{organization_id}/{artifact_id}.mp4"
    assert stored.sha256 == sha256(content).hexdigest()
    assert stored.byte_size == len(content)
    assert downloaded == content
    assert [request.method for request in requests] == ["PUT", "GET", "DELETE"]
    assert all(
        request.url.path == f"/aurora-media/{organization_id}/{artifact_id}.mp4"
        for request in requests
    )
    authorization = requests[0].headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 ")
    assert "Credential=test-access/" in authorization
    assert "SignedHeaders=" in authorization
    assert requests[0].headers["x-amz-security-token"] == "test-session"
    assert requests[0].headers["x-amz-content-sha256"] == sha256(content).hexdigest()
    assert verify_sigv4_signature(requests[0], secret_access_key="test-secret")
    await storage.aclose()


@pytest.mark.anyio
async def test_s3_storage_rejects_oversized_upload_before_network_request():
    requested = False

    async def handle(request):
        nonlocal requested
        requested = True
        return httpx.Response(200)

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://objects.example.com",
        bucket="aurora-media",
        region="us-east-1",
        access_key_id="test-access",
        secret_access_key="test-secret",
        max_object_bytes=4,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(MediaObjectTooLarge):
        await storage.put(
            organization_id=uuid4(),
            artifact_id=uuid4(),
            extension="mp4",
            mime_type="video/mp4",
            content=b"too large",
        )

    assert requested is False
    await storage.aclose()


@pytest.mark.anyio
async def test_s3_storage_bounds_download_using_content_length():
    async def handle(request):
        return httpx.Response(
            200,
            headers={"content-length": "100"},
            content=b"",
        )

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://objects.example.com",
        bucket="aurora-media",
        region="us-east-1",
        access_key_id="test-access",
        secret_access_key="test-secret",
        max_object_bytes=16,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(MediaObjectTooLarge):
        await storage.get(f"{uuid4()}/{uuid4()}.mp4")
    await storage.aclose()


@pytest.mark.anyio
async def test_s3_storage_rejects_invalid_key_without_network_request():
    requested = False

    async def handle(request):
        nonlocal requested
        requested = True
        return httpx.Response(200)

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://objects.example.com",
        bucket="aurora-media",
        region="us-east-1",
        access_key_id="test-access",
        secret_access_key="test-secret",
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(UnsafeMediaStorageKey):
        await storage.get("../../secrets.txt")

    assert requested is False
    await storage.aclose()


@pytest.mark.anyio
async def test_s3_storage_maps_not_found_and_deletes_idempotently():
    async def handle(request):
        return httpx.Response(404)

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://objects.example.com",
        bucket="aurora-media",
        region="us-east-1",
        access_key_id="test-access",
        secret_access_key="test-secret",
        transport=httpx.MockTransport(handle),
    )
    key = f"{uuid4()}/{uuid4()}.mp4"

    with pytest.raises(FileNotFoundError):
        await storage.get(key)
    await storage.delete(key)
    await storage.aclose()


@pytest.mark.anyio
async def test_s3_storage_does_not_expose_remote_error_body():
    async def handle(request):
        return httpx.Response(403, text="secret provider diagnostic")

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://objects.example.com",
        bucket="aurora-media",
        region="us-east-1",
        access_key_id="test-access",
        secret_access_key="test-secret",
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(MediaStorageError, match="HTTP 403") as error:
        await storage.get(f"{uuid4()}/{uuid4()}.mp4")

    assert "secret provider diagnostic" not in str(error.value)
    await storage.aclose()


@pytest.mark.anyio
async def test_s3_storage_rejects_body_that_differs_from_declared_content_length():
    async def handle(request):
        return httpx.Response(
            200,
            headers={"content-length": "3"},
            content=b"four",
        )

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://objects.example.com",
        bucket="aurora-media",
        region="us-east-1",
        access_key_id="test-access",
        secret_access_key="test-secret",
        max_object_bytes=16,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(MediaStorageError, match="does not match its declared content length"):
        await storage.get(f"{uuid4()}/{uuid4()}.mp4")
    await storage.aclose()


@pytest.mark.anyio
async def test_s3_storage_lists_stale_objects_with_signed_paginated_requests():
    organization_id = uuid4()
    artifact_id = uuid4()
    key = f"{organization_id}/{artifact_id}.mp4"
    old_time = (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    requests = []

    async def handle(request):
        requests.append(request)
        token = request.url.params.get("continuation-token")
        if token is None:
            body = (
                "<ListBucketResult><IsTruncated>true</IsTruncated>"
                "<NextContinuationToken>next-page</NextContinuationToken></ListBucketResult>"
            )
        else:
            body = (
                "<ListBucketResult><IsTruncated>false</IsTruncated><Contents>"
                f"<Key>{key}</Key><LastModified>{old_time}</LastModified>"
                "</Contents></ListBucketResult>"
            )
        return httpx.Response(200, text=body, headers={"content-type": "application/xml"})

    storage = S3CompatibleMediaStorage(
        endpoint_url="https://objects.example.com",
        bucket="aurora-media",
        region="us-east-1",
        access_key_id="test-access",
        secret_access_key="test-secret",
        transport=httpx.MockTransport(handle),
    )

    objects = await storage.list_objects(
        older_than=datetime.now(UTC) - timedelta(days=1),
        limit=10,
    )

    assert [item.storage_key for item in objects] == [key]
    assert len(requests) == 2
    assert requests[0].url.params["list-type"] == "2"
    assert requests[1].url.params["continuation-token"] == "next-page"
    assert all(
        verify_sigv4_signature(request, secret_access_key="test-secret")
        for request in requests
    )
    await storage.aclose()
