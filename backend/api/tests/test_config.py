import pytest

from app.core.config import Settings, get_settings


def test_trend_discovery_is_disabled_by_default_and_explicitly_opt_in() -> None:
    assert Settings(_env_file=None).trend_discovery_provider == "none"
    assert (
        Settings(_env_file=None, trend_discovery_provider="hacker_news")
        .trend_discovery_provider
        == "hacker_news"
    )


def test_default_settings(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_name == "Aurora Studio"
    assert settings.app_env == "development"
    assert settings.debug is False
    assert settings.log_level == "INFO"

    get_settings.cache_clear()


@pytest.mark.parametrize(
    "base_url",
    ["http://host.docker.internal:8188", "http://comfyui.wsl.host:8188"],
)
def test_comfyui_allows_explicit_local_runtime_hosts(base_url: str) -> None:
    settings = Settings(
        _env_file=None,
        media_visual_provider="comfyui",
        media_comfyui_base_url=base_url,
        media_comfyui_workflow_path="/app/config/comfyui/workflow_api.json",
        media_comfyui_output_node_id="9",
    )

    assert settings.media_comfyui_base_url == base_url


def test_comfyui_rejects_unapproved_http_host() -> None:
    with pytest.raises(ValueError, match="explicitly allowed local"):
        Settings(
            _env_file=None,
            media_visual_provider="comfyui",
            media_comfyui_base_url="http://192.168.1.20:8188",
            media_comfyui_workflow_path="/app/config/comfyui/workflow_api.json",
            media_comfyui_output_node_id="9",
        )


def test_environment_variables_override_defaults(monkeypatch) -> None:
    get_settings.cache_clear()

    monkeypatch.setenv("APP_NAME", "Aurora Studio Test")
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = get_settings()

    assert settings.app_name == "Aurora Studio Test"
    assert settings.app_env == "testing"
    assert settings.debug is True
    assert settings.log_level == "DEBUG"

    get_settings.cache_clear()


def test_email_outbox_encryption_key_accepts_valid_hexadecimal() -> None:
    key = "a" * 64

    settings = Settings(
        _env_file=None,
        email_outbox_encryption_key=key,
    )

    assert settings.email_outbox_encryption_key is not None
    assert settings.email_outbox_encryption_key.get_secret_value() == key


def test_email_outbox_encryption_key_rejects_invalid_length() -> None:
    with pytest.raises(ValueError, match="exactly 64 hexadecimal characters"):
        Settings(
            _env_file=None,
            email_outbox_encryption_key="too-short",
        )


def test_email_outbox_encryption_key_rejects_invalid_hexadecimal() -> None:
    with pytest.raises(ValueError, match="valid hexadecimal"):
        Settings(
            _env_file=None,
            email_outbox_encryption_key="z" * 64,
        )


def test_production_requires_email_outbox_encryption_key(monkeypatch) -> None:
    monkeypatch.delenv("EMAIL_OUTBOX_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="required in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@resend.dev",
        )


def test_email_outbox_encryption_key_loads_from_environment(
    monkeypatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", "a" * 64)

    settings = get_settings()

    assert settings.email_outbox_encryption_key is not None
    assert settings.email_outbox_encryption_key.get_secret_value() == "a" * 64

    get_settings.cache_clear()


def test_email_outbox_worker_default_settings() -> None:
    settings = Settings(_env_file=None)

    assert settings.email_outbox_batch_size == 10
    assert settings.email_outbox_max_concurrency == 5
    assert settings.email_outbox_poll_interval_seconds == 5
    assert settings.email_outbox_max_attempts == 5
    assert settings.email_outbox_initial_retry_seconds == 30
    assert settings.email_outbox_lease_seconds == 600
    assert settings.email_outbox_delivery_timeout_seconds == 60


def test_email_outbox_worker_accepts_custom_settings() -> None:
    settings = Settings(
        _env_file=None,
        email_outbox_batch_size=20,
        email_outbox_max_concurrency=8,
        email_outbox_poll_interval_seconds=10,
        email_outbox_max_attempts=8,
        email_outbox_initial_retry_seconds=60,
        email_outbox_lease_seconds=900,
        email_outbox_delivery_timeout_seconds=120,
    )

    assert settings.email_outbox_batch_size == 20
    assert settings.email_outbox_max_concurrency == 8
    assert settings.email_outbox_poll_interval_seconds == 10
    assert settings.email_outbox_max_attempts == 8
    assert settings.email_outbox_initial_retry_seconds == 60
    assert settings.email_outbox_lease_seconds == 900
    assert settings.email_outbox_delivery_timeout_seconds == 120


@pytest.mark.parametrize(
    ("setting_name", "invalid_value"),
    [
        ("email_outbox_batch_size", 0),
        ("email_outbox_batch_size", 101),
        ("email_outbox_max_concurrency", 0),
        ("email_outbox_max_concurrency", 21),
        ("email_outbox_poll_interval_seconds", 0),
        ("email_outbox_poll_interval_seconds", 301),
        ("email_outbox_max_attempts", 0),
        ("email_outbox_max_attempts", 21),
        ("email_outbox_initial_retry_seconds", 0),
        ("email_outbox_initial_retry_seconds", 3601),
        ("email_outbox_lease_seconds", 0),
        ("email_outbox_lease_seconds", 3601),
    ],
)
def test_email_outbox_worker_rejects_invalid_settings(
    setting_name: str,
    invalid_value: int,
) -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            **{setting_name: invalid_value},
        )


@pytest.mark.parametrize(
    ("lease", "timeout"),
    [
        (300, 300),
        (200, 300),
    ],
)
def test_email_outbox_rejects_timeout_equal_to_or_greater_than_lease(
    lease: int,
    timeout: int,
) -> None:
    with pytest.raises(
        ValueError,
        match="shorter than the outbox lease duration",
    ):
        Settings(
            _env_file=None,
            email_outbox_lease_seconds=lease,
            email_outbox_delivery_timeout_seconds=timeout,
        )


def test_production_requires_resend_api_key() -> None:
    with pytest.raises(ValueError, match="Resend API key is required in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            email_from_address="onboarding@resend.dev",
            email_outbox_encryption_key="b" * 64,
        )


def test_production_requires_email_from_address() -> None:
    with pytest.raises(ValueError, match="Email from address is required in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_outbox_encryption_key="b" * 64,
        )



def test_production_requires_rate_limit_identity_secret():
    with pytest.raises(ValueError, match="Rate-limit identity secret is required"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret=None,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@resend.dev",
            email_outbox_encryption_key="b" * 64,
        )


def test_rate_limit_identity_secret_rejects_short_value():
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Settings(_env_file=None, rate_limit_identity_secret="too-short")


def test_production_rejects_debug_mode():
    with pytest.raises(ValueError, match="Debug mode must be disabled in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            debug=True,
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@resend.dev",
            email_outbox_encryption_key="b" * 64,
        )


def test_production_requires_https_youtube_redirect_uri():
    with pytest.raises(ValueError, match="YouTube redirect URI must be HTTPS"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
            youtube_client_id="client-id",
            youtube_client_secret="client-secret",
            integration_token_encryption_key="d" * 64,
            youtube_redirect_uri="http://api.example.com/api/v1/integrations/youtube/callback",
            youtube_frontend_result_url="https://app.example.com/settings/integrations",
        )


def test_production_requires_youtube_result_url_on_frontend_origin():
    with pytest.raises(ValueError, match="configured frontend HTTPS origin"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
            youtube_client_id="client-id",
            youtube_client_secret="client-secret",
            integration_token_encryption_key="d" * 64,
            youtube_redirect_uri="https://api.example.com/api/v1/integrations/youtube/callback",
            youtube_frontend_result_url="https://attacker.example/settings/integrations",
        )


def test_production_requires_https_frontend_url():
    with pytest.raises(ValueError, match="Frontend URL must be HTTPS"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="http://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
        )



def test_email_outbox_keyring_supports_multiple_versions_and_active_write_key():
    from app.services.email_outbox_crypto import (
        get_active_encryption_key,
        get_encryption_keys,
    )

    old_key = "a" * 64
    new_key = "b" * 64
    settings = Settings(
        _env_file=None,
        email_outbox_encryption_keyring=f'{{"1":"{old_key}","2":"{new_key}"}}',
        email_outbox_active_key_version=2,
    )

    assert get_encryption_keys(settings) == {1: old_key, 2: new_key}
    assert get_active_encryption_key(settings) == (2, new_key)


def test_youtube_keyring_supports_legacy_v1_and_versioned_keys():
    from app.services.youtube_crypto import (
        get_active_encryption_key,
        get_encryption_keys,
    )

    old_key = "c" * 64
    new_key = "d" * 64
    settings = Settings(
        _env_file=None,
        integration_token_encryption_key=old_key,
        integration_token_encryption_keyring=f'{{"2":"{new_key}"}}',
        integration_token_active_key_version=2,
    )

    assert get_encryption_keys(settings) == {1: old_key, 2: new_key}
    assert get_active_encryption_key(settings) == (2, new_key)


@pytest.mark.parametrize(
    "keyring",
    [
        "not-json",
        "[]",
        "{}",
        '{"0":"' + "a" * 64 + '"}',
        '{"01":"' + "a" * 64 + '"}',
        '{"1":"short"}',
        '{"1":"' + "z" * 64 + '"}',
        '{"1":"' + ("a" * 63 + " ") + '"}',
        '{"2147483648":"' + "a" * 64 + '"}',
        '{"1":"' + "a" * 64 + '","1":"' + "b" * 64 + '"}',
    ],
)
def test_encryption_keyring_rejects_malformed_configuration(keyring):
    with pytest.raises(ValueError, match="keyring|keys|versions"):
        Settings(
            _env_file=None,
            email_outbox_encryption_keyring=keyring,
        )


def test_encryption_keyring_rejects_missing_active_version():
    with pytest.raises(ValueError, match="active encryption key version 2 is missing"):
        Settings(
            _env_file=None,
            email_outbox_encryption_keyring='{"1":"' + "a" * 64 + '"}',
            email_outbox_active_key_version=2,
        )


def test_legacy_email_encryption_key_maps_to_version_one():
    from app.services.email_outbox_crypto import get_encryption_keys

    key = "e" * 64
    settings = Settings(_env_file=None, email_outbox_encryption_key=key)

    assert get_encryption_keys(settings) == {1: key}


def test_production_requires_versioned_email_keyring_when_legacy_key_absent(monkeypatch):
    monkeypatch.delenv("EMAIL_OUTBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("EMAIL_OUTBOX_ENCRYPTION_KEYRING", raising=False)
    with pytest.raises(
        ValueError,
        match="Email outbox encryption keyring is required in production",
    ):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
        )


@pytest.mark.parametrize("active_version", [0, 2_147_483_648])
def test_youtube_active_encryption_key_version_must_fit_database_integer(active_version):
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            integration_token_active_key_version=active_version,
        )


def test_research_provider_defaults_to_mock_and_accepts_explicit_http_opt_in():
    assert Settings(_env_file=None).research_provider == "mock"
    settings = Settings(_env_file=None, research_provider="wikipedia_http")
    assert settings.research_provider == "wikipedia_http"
    assert Settings(_env_file=None, research_provider="crossref").research_provider == "crossref"
    multi_source = Settings(_env_file=None, research_provider="multi_source")
    assert multi_source.research_provider == "multi_source"


def test_research_provider_rejects_unknown_provider():
    with pytest.raises(ValueError):
        Settings(_env_file=None, research_provider="arbitrary_url_fetcher")


def test_searxng_research_mode_requires_valid_public_https_instance():
    with pytest.raises(ValueError, match="RESEARCH_SEARXNG_BASE_URL is required"):
        Settings(_env_file=None, research_provider="multi_source_searxng")

    configured = Settings(
        _env_file=None,
        research_provider="multi_source_searxng",
        research_searxng_base_url="https://search.example.org/searx",
    )
    assert configured.research_searxng_base_url == "https://search.example.org/searx"

    for unsafe_url in (
        "http://search.example.org",
        "https://127.0.0.1",
        "https://search.local",
        "https://search.example.org/?query=x",
    ):
        with pytest.raises(ValueError):
            Settings(
                _env_file=None,
                research_provider="multi_source_searxng",
                research_searxng_base_url=unsafe_url,
            )



def test_story_worker_settings_are_opt_in_and_configurable() -> None:
    defaults = Settings(_env_file=None)
    assert defaults.story_worker_enabled is False
    assert defaults.story_worker_poll_interval_seconds == 5
    assert defaults.story_worker_lease_seconds == 900
    assert defaults.story_worker_max_attempts == 5

    configured = Settings(
        _env_file=None,
        story_worker_enabled=True,
        story_worker_llm_base_url="http://ollama:11434/v1",
        story_worker_model="qwen2.5:7b",
        story_worker_verification_model="qwen2.5:7b",
    )
    assert configured.story_worker_enabled is True
    assert configured.story_worker_model == "qwen2.5:7b"


def test_production_rejects_http_llm_endpoint_with_api_key() -> None:
    with pytest.raises(ValueError, match="must use HTTPS in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
            story_worker_enabled=True,
            story_worker_llm_base_url="http://llm.internal:11434/v1",
            story_worker_llm_api_key="secret-token",
            story_worker_model="qwen2.5:7b",
        )


def test_production_rejects_http_llm_endpoint_without_api_key() -> None:
    with pytest.raises(ValueError, match="must use HTTPS in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
            story_worker_enabled=True,
            story_worker_llm_base_url="http://llm.internal/v1",
            story_worker_model="qwen2.5:7b",
        )


def test_production_accepts_https_llm_endpoint_with_api_key() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        frontend_url="https://app.example.com",
        jwt_secret_key="a" * 32,
        rate_limit_identity_secret="c" * 64,
        invitation_email_mode="provider",
        resend_api_key="re_test_key",
        email_from_address="onboarding@example.com",
        email_outbox_encryption_key="b" * 64,
        story_worker_enabled=True,
        story_worker_llm_base_url="https://llm.internal/v1",
        story_worker_llm_api_key="secret-token",
        story_worker_model="qwen2.5:7b",
    )

    assert settings.story_worker_llm_base_url == "https://llm.internal/v1"
    assert settings.story_worker_llm_api_key is not None


def test_development_allows_http_llm_endpoint_for_local_ollama() -> None:
    settings = Settings(
        _env_file=None,
        story_worker_enabled=True,
        story_worker_llm_base_url="http://ollama:11434/v1",
        story_worker_model="qwen2.5:7b",
    )

    assert settings.story_worker_llm_base_url == "http://ollama:11434/v1"


def test_huggingface_router_requires_https_endpoint_and_token():
    with pytest.raises(ValueError, match="requires an API key"):
        Settings(
            _env_file=None,
            story_worker_enabled=True,
            story_worker_provider="huggingface_router",
            story_worker_llm_base_url="https://router.huggingface.co/v1",
            story_worker_model="Qwen/Qwen2.5-7B-Instruct",
        )

    configured = Settings(
        _env_file=None,
        story_worker_enabled=True,
        story_worker_provider="huggingface_router",
        story_worker_llm_base_url="https://router.huggingface.co/v1",
        story_worker_llm_api_key="hf-test-token",
        story_worker_model="Qwen/Qwen2.5-7B-Instruct",
    )
    assert configured.story_worker_provider == "huggingface_router"


@pytest.mark.parametrize(
    "base_url",
    ["http://router.huggingface.co/v1", "https://example.com/v1", "https://router.huggingface.co/"],
)
def test_huggingface_router_rejects_noncanonical_endpoint(base_url):
    with pytest.raises(ValueError, match="requires its canonical HTTPS API endpoint"):
        Settings(
            _env_file=None,
            story_worker_provider="huggingface_router",
            story_worker_llm_base_url=base_url,
        )


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai", "https://api.openai.com/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("deepseek", "https://api.deepseek.com"),
        ("huggingface_router", "https://router.huggingface.co/v1"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ("groq", "https://api.groq.com/openai/v1"),
        ("anthropic", "https://api.anthropic.com/v1"),
    ],
)
def test_named_story_provider_accepts_canonical_endpoint(provider, base_url):
    configured = Settings(
        _env_file=None,
        story_worker_enabled=True,
        story_worker_provider=provider,
        story_worker_llm_base_url=base_url,
        story_worker_llm_api_key="test-provider-key",
        story_worker_model="provider-model-id",
    )
    assert configured.story_worker_provider == provider


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai", "https://attacker.example/v1"),
        ("openrouter", "https://openrouter.ai/v1"),
        ("deepseek", "https://api.deepseek.com/v1"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/"),
        ("groq", "https://api.groq.com/v1"),
        ("anthropic", "https://api.anthropic.com/anthropic/v1"),
    ],
)
def test_named_story_provider_rejects_noncanonical_endpoint(provider, base_url):
    with pytest.raises(ValueError, match="requires its canonical HTTPS API endpoint"):
        Settings(
            _env_file=None,
            story_worker_provider=provider,
            story_worker_llm_base_url=base_url,
        )


def test_enabled_named_story_provider_requires_api_key():
    with pytest.raises(ValueError, match="selected hosted story provider requires an API key"):
        Settings(
            _env_file=None,
            story_worker_enabled=True,
            story_worker_provider="deepseek",
            story_worker_llm_base_url="https://api.deepseek.com",
            story_worker_model="deepseek-flash",
        )

def test_media_worker_is_disabled_by_default():
    settings = Settings(_env_file=None)

    assert settings.media_worker_enabled is False
    assert settings.media_worker_poll_interval_seconds == 5
    assert settings.media_worker_lease_seconds == 600
    assert settings.media_worker_max_attempts == 5
    assert settings.media_storage_backend == "local"
    assert settings.media_storage_root.is_absolute()
    assert settings.media_motion_style == "gentle_drift"
    assert settings.media_s3_bucket is None
    assert settings.media_tts_base_url is None
    assert settings.media_video_encoder is None


def test_media_worker_defaults_to_local_abstract_visuals():
    settings = Settings(_env_file=None)

    assert settings.media_visual_provider == "local_abstract"
    assert settings.media_wikimedia_user_agent is None
    assert settings.media_wikimedia_timeout_seconds == 20
    assert settings.media_wikimedia_max_api_response_bytes == 2_000_000
    assert settings.media_wikimedia_max_image_bytes == 20_000_000


def test_media_worker_accepts_explicit_wikimedia_visual_provider():
    settings = Settings(
        _env_file=None,
        media_worker_enabled=True,
        media_visual_provider="wikimedia_commons",
        media_wikimedia_user_agent=(
            "AuroraStudio/1.0 (https://aurora.example; ops@aurora.example)"
        ),
        media_tts_base_url="http://127.0.0.1:8880/v1",
        media_tts_model="kokoro",
        media_tts_voice="af_heart",
        media_tts_rights_basis="licensed",
        media_tts_license_id="Apache-2.0",
        media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
        media_tts_source_uri="https://example.com/kokoro-model-and-voice",
        media_tts_attribution="Kokoro model and voice pack attribution",
        media_video_encoder="libx264",
    )

    assert settings.media_visual_provider == "wikimedia_commons"


def test_media_worker_requires_tts_license_uri_for_licensed_voice():
    with pytest.raises(ValueError, match="MEDIA_TTS_LICENSE_URI"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="licensed",
            media_tts_license_id="Apache-2.0",
            media_tts_source_uri="https://example.com/kokoro-model-and-voice",
            media_tts_attribution="Kokoro model and voice pack attribution",
            media_video_encoder="libx264",
        )


def test_media_worker_requires_tts_source_uri():
    with pytest.raises(ValueError, match="MEDIA_TTS_SOURCE_URI"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="licensed",
            media_tts_license_id="Apache-2.0",
            media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
            media_tts_attribution="Kokoro model and voice pack attribution",
            media_video_encoder="libx264",
        )


def test_media_worker_rejects_non_https_tts_license_uri():
    with pytest.raises(ValueError, match="canonical HTTPS URL"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="licensed",
            media_tts_license_id="Apache-2.0",
            media_tts_license_uri="http://example.com/license",
            media_tts_source_uri="https://example.com/kokoro-model-and-voice",
            media_tts_attribution="Kokoro model and voice pack attribution",
            media_video_encoder="libx264",
        )


def test_media_worker_requires_contact_user_agent_for_wikimedia_provider():
    with pytest.raises(ValueError, match="MEDIA_WIKIMEDIA_USER_AGENT"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_visual_provider="wikimedia_commons",
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="licensed",
            media_tts_license_id="Apache-2.0",
            media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
            media_tts_source_uri="https://example.com/kokoro-model-and-voice",
            media_tts_attribution="Kokoro model and voice pack attribution",
            media_video_encoder="libx264",
        )


def test_media_worker_requires_explicit_provider_and_encoder_configuration():
    with pytest.raises(ValueError, match="MEDIA_TTS_BASE_URL"):
        Settings(_env_file=None, media_worker_enabled=True)


def test_media_worker_accepts_explicit_local_tts_and_encoder_settings(tmp_path):
    settings = Settings(
        _env_file=None,
        media_worker_enabled=True,
        media_storage_root=tmp_path / "media",
        media_tts_base_url="http://127.0.0.1:8880/v1",
        media_tts_model="kokoro",
        media_tts_voice="af_heart",
        media_tts_rights_basis="licensed",
        media_tts_license_id="Apache-2.0",
        media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
        media_tts_source_uri="https://example.com/kokoro-model-and-voice",
        media_tts_attribution="Kokoro model and voice pack attribution",
        media_video_encoder="libx264",
    )

    assert settings.media_worker_enabled is True
    assert settings.media_tts_base_url == "http://127.0.0.1:8880/v1"
    assert settings.media_video_encoder == "libx264"


def test_media_worker_accepts_explicit_s3_storage_settings():
    settings = Settings(
        _env_file=None,
        media_worker_enabled=True,
        media_storage_backend="s3",
        media_s3_endpoint_url="http://minio:9000",
        media_s3_bucket="aurora-media",
        media_s3_region="us-east-1",
        media_s3_access_key_id="test-access",
        media_s3_secret_access_key="test-secret",
        media_tts_base_url="http://127.0.0.1:8880/v1",
        media_tts_model="kokoro",
        media_tts_voice="af_heart",
        media_tts_rights_basis="licensed",
        media_tts_license_id="Apache-2.0",
        media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
        media_tts_source_uri="https://example.com/kokoro-model-and-voice",
        media_tts_attribution="TTS model and voice pack attribution",
        media_video_encoder="libx264",
    )

    assert settings.media_storage_backend == "s3"
    assert settings.media_s3_endpoint_url == "http://minio:9000"
    assert settings.media_s3_bucket == "aurora-media"
    assert settings.media_s3_region == "us-east-1"


def test_media_worker_rejects_incomplete_s3_storage_settings():
    with pytest.raises(ValueError, match="MEDIA_S3_ENDPOINT_URL"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_storage_backend="s3",
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="cc0",
            media_tts_license_id="CC0 1.0",
            media_tts_license_uri="https://creativecommons.org/publicdomain/zero/1.0/",
            media_tts_source_uri="https://example.com/kokoro-model-and-voice",
            media_video_encoder="libx264",
        )


def test_production_s3_storage_requires_https_endpoint():
    with pytest.raises(ValueError, match="Media S3 endpoint must use HTTPS in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
            media_worker_enabled=True,
            media_storage_backend="s3",
            media_s3_endpoint_url="http://objects.example.com",
            media_s3_bucket="aurora-media",
            media_s3_region="auto",
            media_s3_access_key_id="test-access",
            media_s3_secret_access_key="test-secret",
            media_tts_base_url="https://tts.example.com/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="cc0",
            media_tts_license_id="CC0 1.0",
            media_tts_license_uri="https://creativecommons.org/publicdomain/zero/1.0/",
            media_tts_source_uri="https://example.com/kokoro-model-and-voice",
            media_video_encoder="libx264",
        )


def test_media_worker_rejects_relative_storage_root():
    with pytest.raises(ValueError, match="absolute path"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_storage_root="relative/media",
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="licensed",
            media_tts_license_id="Apache-2.0",
            media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
            media_tts_source_uri="https://example.com/kokoro-model-and-voice",
            media_tts_attribution="Kokoro model and voice pack attribution",
            media_video_encoder="libx264",
        )


def test_production_media_worker_requires_https_tts_endpoint():
    with pytest.raises(ValueError, match="must use HTTPS in production"):
        Settings(
            _env_file=None,
            app_env="production",
            frontend_url="https://app.example.com",
            jwt_secret_key="a" * 32,
            rate_limit_identity_secret="c" * 64,
            invitation_email_mode="provider",
            resend_api_key="re_test_key",
            email_from_address="onboarding@example.com",
            email_outbox_encryption_key="b" * 64,
            media_worker_enabled=True,
            media_storage_root="/var/lib/aurora/media",
            media_tts_base_url="http://tts.internal/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="licensed",
            media_tts_license_id="Apache-2.0",
            media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
            media_tts_source_uri="https://example.com/kokoro-model-and-voice",
            media_tts_attribution="Kokoro model and voice pack attribution",
            media_video_encoder="libx264",
        )

def test_media_worker_requires_license_identifier_for_licensed_tts():
    with pytest.raises(ValueError, match="MEDIA_TTS_LICENSE_ID"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="licensed",
            media_video_encoder="libx264",
        )


def test_media_worker_requires_attribution_for_cc_by_tts():
    with pytest.raises(ValueError, match="MEDIA_TTS_ATTRIBUTION"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_tts_base_url="http://127.0.0.1:8880/v1",
            media_tts_model="kokoro",
            media_tts_voice="af_heart",
            media_tts_rights_basis="cc_by",
            media_tts_license_id="CC-BY-4.0",
            media_video_encoder="libx264",
        )



def test_enabled_wikimedia_worker_requires_real_contact_information(tmp_path):
    with pytest.raises(ValueError, match="MEDIA_WIKIMEDIA_USER_AGENT"):
        Settings(
            _env_file=None,
            media_worker_enabled=True,
            media_tts_base_url="http://127.0.0.1:8000/v1",
            media_tts_model="test-model",
            media_tts_voice="test-voice",
            media_video_encoder="libx264",
            media_tts_rights_basis="licensed",
            media_tts_license_id="test-license",
            media_tts_license_uri="https://example.com/test-license",
            media_tts_source_uri="https://example.com/test-voice",
            media_tts_attribution="Test voice attribution",
            media_storage_root=tmp_path / "media",
            media_visual_provider="wikimedia_commons",
            media_wikimedia_user_agent="AuroraStudio/1.0 (operator contact)",
        )



def test_curated_rss_trend_discovery_requires_operator_feed_configuration():
    with pytest.raises(ValueError, match="At least one TREND_DISCOVERY_FEEDS"):
        Settings(_env_file=None, trend_discovery_provider="curated_rss")


def test_curated_rss_trend_discovery_accepts_bounded_https_feed_configuration():
    settings = Settings(
        _env_file=None,
        trend_discovery_provider="curated_rss",
        trend_discovery_feeds=[
            {"url": "https://news.example.com/feed.xml", "publisher": "Example News"}
        ],
    )
    assert settings.trend_discovery_provider == "curated_rss"
    assert settings.trend_discovery_feeds[0]["publisher"] == "Example News"


@pytest.mark.parametrize(
    "feed",
    [
        {"url": "http://news.example.com/feed.xml", "publisher": "Example"},
        {"url": "https://user:pass@news.example.com/feed.xml", "publisher": "Example"},
        {"url": "https://news.example.com/feed.xml#fragment", "publisher": "Example"},
        {"url": "https://news.example.com/feed.xml", "publisher": ""},
        {"url": "https://news.example.com/feed.xml", "publisher": "x" * 201},
        {"url": "https://news.example.com/feed.xml", "publisher": "Example", "extra": "no"},
    ],
)
def test_curated_rss_trend_discovery_rejects_unsafe_or_malformed_feed_configuration(feed):
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            trend_discovery_provider="curated_rss",
            trend_discovery_feeds=[feed],
        )


def test_curated_rss_trend_discovery_rejects_duplicate_feed_urls():
    feed = {"url": "https://news.example.com/feed.xml", "publisher": "Example"}
    with pytest.raises(ValueError, match="must be unique"):
        Settings(
            _env_file=None,
            trend_discovery_provider="curated_rss",
            trend_discovery_feeds=[feed, feed],
        )


def test_curated_rss_trend_discovery_limits_feed_count():
    feeds = [
        {"url": f"https://news{index}.example.com/feed.xml", "publisher": "Example"}
        for index in range(11)
    ]
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            trend_discovery_provider="curated_rss",
            trend_discovery_feeds=feeds,
        )


def test_youtube_publish_worker_is_disabled_without_explicit_opt_in() -> None:
    settings = Settings(_env_file=None)

    assert settings.youtube_publish_worker_enabled is False
    assert settings.youtube_egress_proxy_url is None
    assert settings.youtube_upload_chunk_size_bytes == 8_388_608


def test_youtube_publish_worker_requires_oauth_encryption_and_allowlisted_proxy() -> None:
    with pytest.raises(ValueError, match="YouTube OAuth client credentials"):
        Settings(_env_file=None, youtube_publish_worker_enabled=True)

    with pytest.raises(ValueError, match="HTTP egress proxy on port 3128"):
        Settings(
            _env_file=None,
            youtube_publish_worker_enabled=True,
            youtube_client_id="client-id",
            youtube_client_secret="client-secret",
            integration_token_encryption_key="a" * 64,
            youtube_egress_proxy_url="https://proxy.example.com:3128",
        )

    settings = Settings(
        _env_file=None,
        youtube_publish_worker_enabled=True,
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
        integration_token_encryption_key="a" * 64,
        youtube_egress_proxy_url="http://youtube-egress-proxy:3128",
    )
    assert settings.youtube_publish_worker_enabled is True


def test_youtube_upload_chunk_size_must_align_to_google_resumable_chunks() -> None:
    with pytest.raises(ValueError, match="multiple of 256 KiB"):
        Settings(
            _env_file=None,
            youtube_upload_chunk_size_bytes=1_000_000,
        )


def test_media_s3_proxy_requires_a_private_http_proxy_on_standard_port() -> None:
    settings = Settings(
        _env_file=None,
        media_s3_proxy_url="http://media-egress-proxy:3128",
    )
    assert settings.media_s3_proxy_url == "http://media-egress-proxy:3128"

    for value in (
        "https://media-egress-proxy:3128",
        "http://user:password@media-egress-proxy:3128",
        "http://media-egress-proxy:8080",
    ):
        with pytest.raises(ValueError, match="Media S3 proxy URL"):
            Settings(_env_file=None, media_s3_proxy_url=value)

def test_metrics_endpoint_requires_a_long_bearer_token():
    with pytest.raises(ValueError, match="METRICS_BEARER_TOKEN"):
        Settings(
            _env_file=None,
            metrics_enabled=True,
            metrics_bearer_token="too-short",
        )
