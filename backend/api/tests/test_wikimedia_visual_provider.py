import struct

import httpx
import pytest

from app.media.wikimedia import (
    WikimediaCommonsVisualProvider,
    WikimediaVisualProviderError,
    _search_query,
)

IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/example.jpg"

def make_jpeg(width, height):
    return (
        bytes.fromhex("ffd8ffc0")
        + bytes.fromhex("0011")
        + bytes([8])
        + struct.pack(">HH", height, width)
        + bytes([3])
        + bytes.fromhex("011100021100031100")
        + bytes.fromhex("ffd9")
    )



def commons_payload(
    *,
    license_name="CC BY 4.0",
    license_url="https://creativecommons.org/licenses/by/4.0/",
    artist='<a href="https://commons.wikimedia.org/wiki/User:Jane">Jane Doe</a>',
    attribution_required="true",
    thumbnail_url=IMAGE_URL,
    thumbnail_mime="image/jpeg",
    thumbnail_width=640,
    thumbnail_height=360,
):
    return {
        "query": {
            "pages": {
                "1": {
                    "index": 1,
                    "title": "File:Example science image.jpg",
                    "imageinfo": [
                        {
                            "thumburl": thumbnail_url,
                            "descriptionurl": (
                                "https://commons.wikimedia.org/wiki/File:Example_science_image.jpg"
                            ),
                            "thumbmime": thumbnail_mime,
                            "mime": "image/jpeg",
                            "thumbwidth": thumbnail_width,
                            "thumbheight": thumbnail_height,
                            "extmetadata": {
                                "LicenseShortName": {"value": license_name},
                                "LicenseUrl": {"value": license_url},
                                "Artist": {"value": artist},
                                "Credit": {"value": ""},
                                "AttributionRequired": {"value": attribution_required},
                                "Restrictions": {"value": ""},
                            },
                        }
                    ],
                }
            }
        }
    }


@pytest.mark.anyio
async def test_wikimedia_provider_returns_cc_by_image_with_complete_attribution():
    image_content = make_jpeg(640, 360)

    def handler(request):
        assert request.headers["user-agent"] == (
            "AuroraStudio/1.0 (https://aurora.example; ops@aurora.example)"
        )
        if request.url.host == "commons.wikimedia.org":
            assert request.url.path == "/w/api.php"
            assert request.url.params["gsrnamespace"] == "6"
            assert request.url.params["gsrlimit"] == "12"
            assert "science" in request.url.params["gsrsearch"].lower()
            if "incategory:" in request.url.params["gsrsearch"]:
                assert "incategory:CC-BY-4.0" in request.url.params["gsrsearch"]
            return httpx.Response(200, json=commons_payload())
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=image_content,
            )
        return httpx.Response(404)

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (https://aurora.example; ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        asset = await provider.generate(
            visual_direction="A science laboratory",
            narration_segment="Researchers discover a new pattern.",
            scene_index=0,
        )
    finally:
        await provider.aclose()

    assert asset.content == image_content
    assert asset.mime_type == "image/jpeg"
    assert (asset.width, asset.height) == (640, 360)
    assert asset.provider_id == "wikimedia_commons"
    assert asset.rights_basis == "cc_by"
    assert asset.license_id == "CC BY 4.0"
    assert asset.license_uri == "https://creativecommons.org/licenses/by/4.0/"
    assert asset.source_uri.startswith("https://commons.wikimedia.org/wiki/File:")
    assert "Jane Doe" in asset.attribution
    assert "CC BY 4.0" in asset.attribution
    assert "resized/cropped for video" in asset.attribution


@pytest.mark.anyio
async def test_wikimedia_provider_accepts_cc0_without_artist_attribution():
    image_content = make_jpeg(640, 360)
    payload = commons_payload(
        license_name="CC0 1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        artist="",
        attribution_required="false",
    )

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=image_content,
        )

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        asset = await provider.generate(
            visual_direction="A historical building",
            narration_segment="A story about architecture.",
            scene_index=1,
        )
    finally:
        await provider.aclose()

    assert asset.rights_basis == "cc0"
    assert asset.license_id == "CC0 1.0"
    assert asset.attribution is not None


@pytest.mark.anyio
async def test_wikimedia_provider_accepts_current_thumb_host_and_width_query():
    image_content = make_jpeg(640, 360)
    payload = commons_payload(
        thumbnail_url=(
            "https://thumb.wikimedia.org/wikipedia/commons/thumb/example.jpg?width=640"
            "&utm_source=commons.wikimedia.org&utm_campaign=imageinfo"
            "&utm_content=thumbnail"
        ),
        thumbnail_mime=None,
    )

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=payload)
        if request.url.host == "thumb.wikimedia.org":
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=image_content,
            )
        return httpx.Response(404)

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        asset = await provider.generate(
            visual_direction="James Webb Space Telescope",
            narration_segment="A telescope in space.",
            scene_index=0,
        )
    finally:
        await provider.aclose()

    assert asset.content == image_content
    assert asset.mime_type == "image/jpeg"


@pytest.mark.anyio
async def test_wikimedia_provider_uses_verified_image_dimensions_when_metadata_differs():
    image_content = make_jpeg(640, 360)
    payload = commons_payload(thumbnail_width=1024, thumbnail_height=576)

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=image_content,
        )

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        asset = await provider.generate(
            visual_direction="A historical building",
            narration_segment="A story about architecture.",
            scene_index=0,
        )
    finally:
        await provider.aclose()

    assert (asset.width, asset.height) == (640, 360)


@pytest.mark.anyio
async def test_wikimedia_provider_rejects_sharealike_license():
    payload = commons_payload(
        license_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0/",
    )

    def handler(request):
        return httpx.Response(200, json=payload)

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_wikimedia_provider_rejects_non_allowlisted_thumbnail_host():
    payload = commons_payload(thumbnail_url="https://attacker.example/image.jpg")

    def handler(request):
        return httpx.Response(200, json=payload)

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_wikimedia_provider_rejects_cc_by_without_attribution():
    payload = commons_payload(artist="", attribution_required="true")

    def handler(request):
        return httpx.Response(200, json=payload)

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_wikimedia_provider_enforces_image_size_limit():
    payload = commons_payload()

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"\xff\xd8\xff" + b"x" * 32,
        )

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        max_image_bytes=16,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "user_agent",
    (
        "AuroraStudio/1.0",
        "AuroraStudio/1.0 (operator contact)",
        "AuroraStudio/1.0 (mailto:ops@invalid)",
    ),
)
def test_wikimedia_provider_requires_contact_bearing_user_agent(user_agent):
    with pytest.raises(ValueError, match="contact information"):
        WikimediaCommonsVisualProvider(user_agent=user_agent)


def test_wikimedia_provider_accepts_https_contact_url():
    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (https://aurora.example/contact)"
    )
    assert provider is not None


def test_wikimedia_search_query_preserves_unicode_terms():
    query = _search_query("தமிழ் வரலாறு", "भारत की संस्कृति")

    assert "தமிழ்" in query
    assert "भारत" in query


@pytest.mark.anyio
async def test_wikimedia_provider_rejects_unapproved_license_url_path():
    payload = commons_payload(
        license_url="https://creativecommons.org/licenses/by/4.0.evil/",
    )

    def handler(request):
        return httpx.Response(200, json=payload)

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_wikimedia_provider_rejects_untrusted_source_page():
    payload = commons_payload()
    payload["query"]["pages"]["1"]["imageinfo"][0]["descriptionurl"] = (
        "https://attacker.example/wiki/File:Example.jpg"
    )

    def handler(request):
        return httpx.Response(200, json=payload)

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_wikimedia_provider_bounds_api_response_body():
    def handler(request):
        return httpx.Response(200, json=commons_payload())

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        max_api_response_bytes=16,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="size limit"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_wikimedia_provider_does_not_follow_image_redirects():
    payload = commons_payload()

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=payload)
        return httpx.Response(
            302,
            headers={"location": "https://attacker.example/image.jpg"},
        )

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A science laboratory",
                narration_segment="Scientific research",
                scene_index=0,
            )
    finally:
        await provider.aclose()


def test_wikimedia_provider_rejects_user_agent_with_newline():
    with pytest.raises(ValueError, match="contact information"):
        WikimediaCommonsVisualProvider(
            user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)" + chr(10) + "X-Injected: yes"
        )

@pytest.mark.anyio
async def test_wikimedia_provider_rejects_low_resolution_thumbnail():
    payload = commons_payload(thumbnail_width=240, thumbnail_height=320)
    image_content = make_jpeg(240, 320)

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=image_content,
        )

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError, match="No Wikimedia result"):
            await provider.generate(
                visual_direction="A low resolution image",
                narration_segment="Test narration",
                scene_index=0,
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_wikimedia_provider_skips_pdf_page_thumbnail():
    payload = commons_payload()
    page = payload["query"]["pages"]["1"]["imageinfo"][0]
    page["mime"] = "application/pdf"

    def handler(request):
        if request.url.host == "commons.wikimedia.org":
            return httpx.Response(200, json=payload)
        pytest.fail("A document preview must not be downloaded as a scene photo.")

    provider = WikimediaCommonsVisualProvider(
        user_agent="AuroraStudio/1.0 (mailto:ops@aurora.example)",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WikimediaVisualProviderError):
            await provider.generate(
                visual_direction="Moon exploration",
                narration_segment="A science story.",
                scene_index=0,
            )
    finally:
        await provider.aclose()

