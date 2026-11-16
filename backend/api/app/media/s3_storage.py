"""Private S3-compatible object storage using AWS Signature Version 4."""

import hashlib
import hmac
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit
from uuid import UUID
from xml.etree import ElementTree

import httpx

from app.media.storage import (
    _ALLOWED_EXTENSIONS,
    MediaObjectTooLarge,
    MediaStorageError,
    StoredMediaObject,
    StoredMediaObjectInfo,
    validate_media_storage_key,
)

_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")


class S3CompatibleMediaStorage:
    """Private path-style S3 storage with bounded downloads and SigV4 requests.

    The adapter supports AWS S3, Cloudflare R2 and MinIO-compatible endpoints.
    It never constructs public URLs or changes bucket/object ACLs.
    """

    backend = "s3"

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        session_token: str | None = None,
        max_object_bytes: int = 100_000_000,
        timeout_seconds: float = 30.0,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(endpoint_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("S3 endpoint must be an HTTP(S) origin without path or credentials.")
        if (
            not _BUCKET_PATTERN.fullmatch(bucket)
            or ".." in bucket
            or ".-" in bucket
            or "-." in bucket
            or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", bucket) is not None
        ):
            raise ValueError("S3 bucket name is invalid.")
        if not _REGION_PATTERN.fullmatch(region):
            raise ValueError("S3 region is invalid.")
        if not access_key_id.strip() or not secret_access_key.strip():
            raise ValueError("S3 access key ID and secret access key are required.")
        if session_token is not None and not session_token.strip():
            raise ValueError("S3 session token cannot be empty.")
        if max_object_bytes <= 0 or timeout_seconds <= 0:
            raise ValueError("S3 object limit and request timeout must be positive.")

        self._endpoint_url = endpoint_url.rstrip("/")
        self._bucket = bucket
        self._region = region
        self._access_key_id = access_key_id.strip()
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._max_object_bytes = max_object_bytes
        self._listing_continuation_token: str | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            proxy=proxy_url,
            transport=transport,
        )

    @staticmethod
    def _signing_key(secret_access_key: str, date_stamp: str, region: str) -> bytes:
        date_key = hmac.new(
            ("AWS4" + secret_access_key).encode("utf-8"),
            date_stamp.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()

    def _signed_request(
        self,
        *,
        method: str,
        storage_key: str | None,
        content: bytes = b"",
        content_type: str | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        if storage_key is None:
            canonical_uri = f"/{self._bucket}/"
        else:
            validate_media_storage_key(storage_key)
            canonical_uri = quote(f"/{self._bucket}/{storage_key}", safe="/-_.~")
        encoded_query = ""
        if query:
            encoded_pairs = sorted(
                (quote(key, safe="-_.~"), quote(value, safe="-_.~"))
                for key, value in query.items()
            )
            encoded_query = "&".join(f"{key}={value}" for key, value in encoded_pairs)
        url = f"{self._endpoint_url}{canonical_uri}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        host = urlsplit(url).netloc.lower()
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(content).hexdigest()

        canonical_headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type is not None:
            canonical_headers["content-type"] = content_type
        if self._session_token is not None:
            canonical_headers["x-amz-security-token"] = self._session_token

        signed_headers = ";".join(sorted(canonical_headers))
        canonical_header_block = "".join(
            f"{name}:{value.strip()}\n"
            for name, value in sorted(canonical_headers.items())
        )
        canonical_request = "\n".join(
            (
                method,
                canonical_uri,
                encoded_query,
                canonical_header_block,
                signed_headers,
                payload_hash,
            )
        )
        credential_scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        signing_key = self._signing_key(
            self._secret_access_key,
            date_stamp,
            self._region,
        )
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = dict(canonical_headers)
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return url, headers

    async def _stream_request(
        self,
        *,
        method: str,
        storage_key: str | None,
        content: bytes = b"",
        content_type: str | None = None,
        query: dict[str, str] | None = None,
    ) -> bytes:
        url, headers = self._signed_request(
            method=method,
            storage_key=storage_key,
            content=content,
            content_type=content_type,
            query=query,
        )
        try:
            async with self._client.stream(
                method,
                url,
                headers=headers,
                content=content,
            ) as response:
                if method == "GET" and response.status_code == 404:
                    raise FileNotFoundError("Media object does not exist.")
                if method == "DELETE" and response.status_code == 404:
                    return b""
                allowed_statuses = {
                    "PUT": {200, 201, 204},
                    "GET": {200},
                    "DELETE": {200, 202, 204},
                }
                if response.status_code not in allowed_statuses[method]:
                    raise MediaStorageError(
                        f"S3-compatible storage returned HTTP {response.status_code}."
                    )
                if method != "GET":
                    return b""

                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        raise MediaStorageError(
                            "S3-compatible storage returned an invalid content length."
                        ) from None
                    if declared_size < 0 or declared_size > self._max_object_bytes:
                        raise MediaObjectTooLarge(
                            "Stored media object exceeds the configured size limit."
                        )

                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > self._max_object_bytes:
                        raise MediaObjectTooLarge(
                            "Stored media object exceeds the configured size limit."
                        )
                    chunks.extend(chunk)
                if content_length is not None and len(chunks) != declared_size:
                    raise MediaStorageError(
                        "S3-compatible storage returned a body that does not match "
                        "its declared content length."
                    )
                return bytes(chunks)
        except httpx.HTTPError:
            raise MediaStorageError("S3-compatible storage request failed.") from None

    async def put(
        self,
        *,
        organization_id: UUID,
        artifact_id: UUID,
        extension: str,
        mime_type: str,
        content: bytes,
    ) -> StoredMediaObject:
        if extension not in _ALLOWED_EXTENSIONS:
            raise ValueError("Media extension is not supported.")
        if not isinstance(mime_type, str) or not mime_type.strip() or len(mime_type) > 120:
            raise ValueError("Media MIME type is invalid.")
        if not isinstance(content, bytes) or not content:
            raise ValueError("Media content must be non-empty bytes.")
        if len(content) > self._max_object_bytes:
            raise MediaObjectTooLarge("Media object exceeds the configured size limit.")

        storage_key = f"{organization_id}/{artifact_id}.{extension}"
        await self._stream_request(
            method="PUT",
            storage_key=storage_key,
            content=content,
            content_type=mime_type.strip().lower(),
        )
        return StoredMediaObject(
            storage_key=storage_key,
            mime_type=mime_type.strip().lower(),
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
        )

    async def get(self, storage_key: str) -> bytes:
        return await self._stream_request(method="GET", storage_key=storage_key)

    async def delete(self, storage_key: str) -> None:
        await self._stream_request(method="DELETE", storage_key=storage_key)

    async def list_objects(
        self, *, older_than: datetime, limit: int = 500
    ) -> list[StoredMediaObjectInfo]:
        if older_than.tzinfo is None or limit < 1:
            raise ValueError("A timezone-aware cutoff and positive object limit are required.")

        results: list[StoredMediaObjectInfo] = []
        continuation_token = self._listing_continuation_token
        pages_scanned = 0
        while pages_scanned < 5:
            pages_scanned += 1
            query = {"list-type": "2", "max-keys": str(min(1000, limit))}
            if continuation_token is not None:
                query["continuation-token"] = continuation_token
            payload = await self._stream_request(
                method="GET",
                storage_key=None,
                query=query,
            )
            try:
                root = ElementTree.fromstring(payload)
            except ElementTree.ParseError:
                raise MediaStorageError(
                    "S3-compatible storage returned an invalid object listing."
                ) from None

            for entry in root.findall(".//{*}Contents"):
                key_element = entry.find("{*}Key")
                modified_element = entry.find("{*}LastModified")
                if key_element is None or key_element.text is None or modified_element is None:
                    continue
                try:
                    validate_media_storage_key(key_element.text)
                    modified = datetime.fromisoformat(
                        modified_element.text.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue
                if modified.tzinfo is not None and modified < older_than.astimezone(UTC):
                    results.append(
                        StoredMediaObjectInfo(
                            storage_key=key_element.text,
                            last_modified=modified.astimezone(UTC),
                        )
                    )

            truncated = root.findtext(".//{*}IsTruncated")
            if truncated != "true":
                self._listing_continuation_token = None
                break
            next_token = root.findtext(".//{*}NextContinuationToken")
            if not next_token or next_token == continuation_token:
                raise MediaStorageError(
                    "S3-compatible storage returned an invalid pagination token."
                )
            continuation_token = next_token
            self._listing_continuation_token = next_token
            if results:
                break
        return results

    async def aclose(self) -> None:
        await self._client.aclose()
