import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.encryption_keys import (
    parse_encryption_keyring,
    validate_active_key_version,
)
from app.research.safe_http import validate_research_proxy_url, validate_research_url

# Resolve the repository root independently of the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[4]
_WIKIMEDIA_CONTACT_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_WIKIMEDIA_CONTACT_URL = re.compile(r"https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::443)?(?:/[^\s;)]*)?")
_STORY_PROVIDER_ENDPOINTS = {
    "openai": ("api.openai.com", "/v1"),
    "openrouter": ("openrouter.ai", "/api/v1"),
    "deepseek": ("api.deepseek.com", ""),
    "huggingface_router": ("router.huggingface.co", "/v1"),
    "gemini": ("generativelanguage.googleapis.com", "/v1beta/openai"),
    "groq": ("api.groq.com", "/openai/v1"),
    "anthropic": ("api.anthropic.com", "/v1"),
}


class Settings(BaseSettings):
    app_name: str = "Aurora Studio"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    metrics_enabled: bool = False
    metrics_bearer_token: SecretStr | None = None
    operations_alerts_enabled: bool = False
    operations_alert_channel: str = Field(default="email", min_length=1, max_length=40)
    database_url: SecretStr | None = None
    test_database_url: SecretStr | None = None
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_identity_secret: SecretStr | None = None
    auth_login_ip_limit: int = Field(default=30, gt=0, le=10000)
    auth_login_account_limit: int = Field(default=10, gt=0, le=10000)
    auth_registration_ip_limit: int = Field(default=10, gt=0, le=10000)
    auth_verification_resend_ip_limit: int = Field(default=10, gt=0, le=10000)
    research_run_user_limit: int = Field(default=20, gt=0, le=10000)
    research_run_organization_limit: int = Field(default=100, gt=0, le=100000)
    frontend_url: str = "http://localhost:5173"
    browser_session_idle_timeout_minutes: int = Field(default=30, gt=0, le=1440)
    browser_session_absolute_timeout_hours: int = Field(default=12, gt=0, le=168)

    jwt_secret_key: SecretStr | None = None
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(default=30, gt=0, le=1440)

    invitation_email_mode: Literal["console", "provider"] = "console"
    invitation_expire_days: int = Field(default=7, gt=0, le=30)
    resend_api_key: SecretStr | None = None
    email_from_address: str | None = None
    email_reply_to: str | None = None

    # Legacy single-key setting remains supported as key version 1.
    email_outbox_encryption_key: SecretStr | None = None
    email_outbox_encryption_keyring: SecretStr | None = None
    email_outbox_active_key_version: int = Field(default=1, gt=0, le=2_147_483_647)

    youtube_client_id: str | None = None
    youtube_client_secret: SecretStr | None = None
    youtube_redirect_uri: str = "http://localhost:8000/api/v1/integrations/youtube/callback"
    # Legacy single-key setting remains supported as key version 1.
    integration_token_encryption_key: SecretStr | None = None
    integration_token_encryption_keyring: SecretStr | None = None
    integration_token_active_key_version: int = Field(default=1, gt=0, le=2_147_483_647)
    youtube_oauth_state_ttl_seconds: int = Field(default=600, gt=60, le=1800)
    youtube_frontend_result_url: str = "http://localhost:5173/settings/integrations"
    youtube_publish_worker_enabled: bool = False
    youtube_egress_proxy_url: str | None = None
    youtube_upload_chunk_size_bytes: int = Field(
        default=8_388_608,
        ge=262_144,
        le=67_108_864,
    )
    youtube_upload_timeout_seconds: int = Field(default=120, gt=0, le=600)
    youtube_publish_poll_interval_seconds: int = Field(default=30, gt=0, le=900)
    youtube_publish_lease_seconds: int = Field(default=300, gt=30, le=1800)
    youtube_publish_max_attempts: int = Field(default=5, gt=0, le=20)
    youtube_publish_retry_base_seconds: int = Field(default=30, gt=0, le=3600)

    email_outbox_batch_size: int = Field(default=10, gt=0, le=100)
    email_outbox_max_concurrency: int = Field(default=5, gt=0, le=20)
    email_outbox_poll_interval_seconds: int = Field(default=5, gt=0, le=300)
    email_outbox_heartbeat_interval_seconds: int = Field(default=60, gt=0, le=3600)
    email_outbox_max_attempts: int = Field(default=5, gt=0, le=20)
    email_outbox_initial_retry_seconds: int = Field(default=30, gt=0, le=3600)
    email_outbox_lease_seconds: int = Field(default=600, gt=0, le=3600)

    email_outbox_delivery_timeout_seconds: int = Field(
        default=60,
        gt=0,
        le=300,
    )

    research_provider: Literal[
        "mock", "wikipedia_http", "crossref", "multi_source", "multi_source_searxng"
    ] = "mock"
    research_egress_proxy_url: str | None = None
    research_searxng_base_url: str | None = None
    trend_discovery_provider: Literal["none", "hacker_news", "curated_rss", "searxng"] = "none"
    trend_discovery_searxng_base_url: str | None = None
    trend_discovery_searxng_query: str = Field(
        default="science technology research discoveries", min_length=1, max_length=300
    )
    # Each entry is an operator-approved {url, publisher} object.
    trend_discovery_feeds: list[dict[str, str]] = Field(default_factory=list, max_length=10)
    channel_schedule_lead_minutes: int = Field(default=60, ge=0, le=1440)
    research_worker_batch_size: int = Field(default=5, gt=0, le=50)
    research_worker_max_concurrency: int = Field(default=3, gt=0, le=20)
    research_worker_poll_interval_seconds: int = Field(default=5, gt=0, le=300)
    research_worker_heartbeat_interval_seconds: int = Field(default=30, gt=0, le=300)
    research_worker_max_attempts: int = Field(default=5, gt=0, le=20)
    research_worker_initial_retry_seconds: int = Field(default=30, gt=0, le=3600)
    research_worker_max_retry_seconds: int = Field(default=1800, gt=0, le=86400)
    research_worker_lease_seconds: int = Field(default=300, gt=0, le=3600)
    research_worker_provider_timeout_seconds: int = Field(default=120, gt=0, le=1800)
    research_worker_shutdown_timeout_seconds: int = Field(default=120, gt=0, le=150)

    # Story-stage execution is opt-in and requires an explicitly configured LLM endpoint.
    story_worker_enabled: bool = False
    story_worker_provider: Literal[
        "openai_compatible",
        "ollama",
        "openai",
        "openrouter",
        "deepseek",
        "huggingface_router",
        "gemini",
        "groq",
        "anthropic",
    ] = "openai_compatible"
    story_worker_llm_base_url: str | None = None
    story_worker_llm_api_key: SecretStr | None = None
    story_worker_model: str | None = None
    story_worker_verification_model: str | None = None
    story_worker_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = None
    story_worker_timeout_seconds: int = Field(default=120, gt=0, le=600)
    story_worker_poll_interval_seconds: int = Field(default=5, gt=0, le=300)
    # Leave room for the tenant-configurable 600-second LLM request timeout.
    story_worker_lease_seconds: int = Field(default=900, gt=0, le=3600)
    story_worker_max_attempts: int = Field(default=5, gt=0, le=20)

    # Media production is opt-in and requires explicit TTS and FFmpeg configuration.
    media_worker_enabled: bool = False
    media_worker_poll_interval_seconds: int = Field(default=5, gt=0, le=300)
    # TTS and media providers can run for several minutes on local hardware.
    media_worker_lease_seconds: int = Field(default=600, gt=0, le=3600)
    media_worker_max_attempts: int = Field(default=5, gt=0, le=20)
    media_storage_backend: Literal["local", "s3"] = "local"
    media_storage_root: Path = PROJECT_ROOT / "var" / "media"
    media_storage_max_object_bytes: int = Field(default=100_000_000, gt=0, le=1_000_000_000)
    media_s3_endpoint_url: str | None = None
    media_s3_bucket: str | None = Field(
        default=None,
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    )
    media_s3_region: str = Field(
        default="auto",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9-]+$",
    )
    media_s3_access_key_id: SecretStr | None = None
    media_s3_secret_access_key: SecretStr | None = None
    media_s3_session_token: SecretStr | None = None
    media_s3_timeout_seconds: int = Field(default=30, gt=0, le=300)
    media_s3_proxy_url: str | None = None
    media_tts_provider: str = Field(default="openai_compatible", min_length=1, max_length=80)
    media_tts_languages: list[str] = Field(default_factory=list, max_length=100)
    media_tts_voices: list[str] = Field(default_factory=list, max_length=500)
    media_tts_sample_rates_hz: list[int] = Field(
        default_factory=lambda: [8_000, 16_000, 22_050, 24_000, 44_100, 48_000], max_length=20
    )
    media_tts_hosting_mode: Literal["hosted", "self_hosted"] = "self_hosted"
    media_tts_model_license_id: str | None = None
    media_tts_voice_license_id: str | None = None
    media_tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    media_tts_format: Literal["wav"] = "wav"
    media_tts_cost_per_1000_characters_usd: float | None = Field(default=None, ge=0)
    media_tts_base_url: str | None = None
    media_tts_proxy_url: str | None = None
    media_tts_api_key: SecretStr | None = None
    media_tts_model: str | None = None
    media_tts_voice: str | None = None
    media_tts_rights_basis: (
        Literal["generated_in_application", "public_domain", "cc0", "cc_by", "licensed"] | None
    ) = None
    media_tts_license_id: str | None = None
    media_tts_license_uri: str | None = None
    media_tts_source_uri: str | None = None
    media_tts_attribution: str | None = None
    media_tts_timeout_seconds: int = Field(default=90, gt=0, le=300)
    media_tts_max_response_bytes: int = Field(default=25_000_000, gt=0, le=100_000_000)
    media_visual_provider: Literal["local_abstract", "wikimedia_commons", "comfyui"] = (
        "local_abstract"
    )
    media_visual_composer: Literal["pillow", "chromium_html"] = "chromium_html"
    media_visual_proxy_url: str | None = None
    media_comfyui_base_url: str | None = None
    media_comfyui_api_key: SecretStr | None = None
    media_comfyui_workflow_path: Path | None = None
    media_comfyui_output_node_id: str | None = None
    media_comfyui_timeout_seconds: int = Field(default=300, gt=0, le=1800)
    media_comfyui_negative_prompt: str = Field(
        default=(
            "blurry, low quality, text, letters, words, logo, brand mark, flag, "
            "watermark, labels, signature, typography, diagram, distorted anatomy, "
            "duplicated objects"
        ),
        max_length=2000,
    )
    media_comfyui_style_prompt: str = Field(default="", max_length=2000)
    media_chromium_path: str = "chromium"
    media_wikimedia_user_agent: str | None = None
    media_wikimedia_timeout_seconds: int = Field(default=20, gt=0, le=120)
    media_wikimedia_max_api_response_bytes: int = Field(default=2_000_000, gt=0, le=10_000_000)
    media_wikimedia_max_image_bytes: int = Field(default=20_000_000, gt=0, le=50_000_000)
    media_ffmpeg_path: str = "ffmpeg"
    media_ffprobe_path: str = "ffprobe"
    media_video_encoder: str | None = None
    media_audio_encoder: str = "aac"
    media_motion_style: Literal["static", "gentle_drift"] = "gentle_drift"
    media_video_preset: str = "veryfast"
    media_video_crf: int = Field(default=23, ge=0, le=51)

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug_mode(cls, value):
        """Accept common IDE/deployment mode labels without breaking startup."""
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {
                "release",
                "production",
                "prod",
                "false",
                "0",
                "no",
                "off",
            }:
                return False
            if normalized in {"debug", "development", "dev", "true", "1", "yes", "on"}:
                return True
        return value

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_research_egress_proxy(self):
        self.research_egress_proxy_url = validate_research_proxy_url(self.research_egress_proxy_url)
        return self

    @model_validator(mode="after")
    def validate_research_provider_configuration(self):
        if self.research_provider != "multi_source_searxng":
            return self
        base_url = (self.research_searxng_base_url or "").strip()
        if not base_url:
            raise ValueError(
                "RESEARCH_SEARXNG_BASE_URL is required when "
                "RESEARCH_PROVIDER=multi_source_searxng."
            )
        try:
            parsed = validate_research_url(base_url)
        except ValueError as exc:
            raise ValueError("Research SearXNG URL is not a safe public URL.") from exc
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Research SearXNG URL has an invalid port.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise ValueError("Research SearXNG URL must be an HTTPS origin or base path.")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("Research SearXNG URL must use a DNS hostname.")
        self.research_searxng_base_url = base_url.rstrip("/")
        return self

    @model_validator(mode="after")
    def validate_metrics_configuration(self):
        if self.metrics_enabled:
            token = (
                self.metrics_bearer_token.get_secret_value()
                if self.metrics_bearer_token is not None
                else ""
            )
            if len(token) < 32:
                raise ValueError(
                    "METRICS_BEARER_TOKEN must contain at least 32 characters "
                    "when METRICS_ENABLED=true."
                )
        if self.operations_alerts_enabled:
            if (
                self.email_outbox_encryption_key is None
                and self.email_outbox_encryption_keyring is None
            ):
                raise ValueError(
                    "Email outbox encryption keys are required when operations alerts are enabled."
                )
            if self.app_env.strip().lower() == "production" and (
                self.invitation_email_mode != "provider"
                or self.resend_api_key is None
                or not self.email_from_address
            ):
                raise ValueError(
                    "Production operations alerts require provider email mode, "
                    "RESEND_API_KEY and EMAIL_FROM_ADDRESS."
                )
        return self

    @model_validator(mode="after")
    def validate_auth_settings(self):
        environment = self.app_env.strip().lower()

        if self.trend_discovery_provider == "searxng":
            base_url = (self.trend_discovery_searxng_base_url or "").strip()
            if not base_url:
                raise ValueError(
                    "TREND_DISCOVERY_SEARXNG_BASE_URL is required when SearXNG is enabled."
                )
            parsed_search = urlsplit(base_url)
            try:
                port = parsed_search.port
            except ValueError as exc:
                raise ValueError("SearXNG base URL has an invalid port.") from exc
            if (
                parsed_search.scheme not in {"http", "https"}
                or not parsed_search.hostname
                or parsed_search.username is not None
                or parsed_search.password is not None
                or parsed_search.query
                or parsed_search.fragment
                or parsed_search.path not in {"", "/"}
                or port not in {None, 443}
            ):
                raise ValueError("SearXNG base URL must be a valid HTTPS origin.")
            if parsed_search.scheme != "https":
                raise ValueError("SearXNG base URL must use HTTPS.")
            try:
                ipaddress.ip_address(parsed_search.hostname)
            except ValueError:
                pass
            else:
                raise ValueError("SearXNG base URL must use a DNS hostname, not an IP literal.")
            if not self.trend_discovery_searxng_query.strip():
                raise ValueError("TREND_DISCOVERY_SEARXNG_QUERY must not be empty.")

        if self.trend_discovery_provider == "curated_rss":
            if not self.trend_discovery_feeds:
                raise ValueError(
                    "At least one TREND_DISCOVERY_FEEDS entry is required "
                    "when curated_rss trend discovery is enabled."
                )
            seen_feed_urls: set[str] = set()
            for feed in self.trend_discovery_feeds:
                if set(feed) != {"url", "publisher"}:
                    raise ValueError(
                        "Each curated trend feed must contain exactly url and publisher."
                    )
                url = feed["url"].strip()
                publisher = feed["publisher"].strip()
                parsed_feed = urlsplit(url)
                if (
                    not url
                    or parsed_feed.scheme != "https"
                    or not parsed_feed.hostname
                    or parsed_feed.username is not None
                    or parsed_feed.password is not None
                    or parsed_feed.fragment
                ):
                    raise ValueError("Curated trend feed URLs must be valid HTTPS URLs.")
                if not publisher or len(publisher) > 200:
                    raise ValueError(
                        "Curated trend feed publishers must contain 1 to 200 characters."
                    )
                canonical_url = parsed_feed.geturl()
                if canonical_url in seen_feed_urls:
                    raise ValueError("Curated trend feed URLs must be unique.")
                seen_feed_urls.add(canonical_url)

        if environment == "production" and self.debug:
            raise ValueError("Debug mode must be disabled in production.")

        if environment == "production":
            frontend = urlsplit(self.frontend_url)
            if (
                frontend.scheme != "https"
                or not frontend.hostname
                or frontend.username is not None
                or frontend.password is not None
                or frontend.query
                or frontend.fragment
                or frontend.path not in ("", "/")
            ):
                raise ValueError(
                    "Frontend URL must be HTTPS without credentials, "
                    "path, query, or fragment in production."
                )

        if self.jwt_secret_key is not None:
            secret = self.jwt_secret_key.get_secret_value()
            if len(secret.encode("utf-8")) < 32:
                raise ValueError("JWT secret key must contain at least 32 bytes.")

        if environment == "production" and self.jwt_secret_key is None:
            raise ValueError("JWT secret key is required in production.")
        if self.rate_limit_identity_secret is not None:
            rate_limit_secret = self.rate_limit_identity_secret.get_secret_value()
            if len(rate_limit_secret.encode("utf-8")) < 32:
                raise ValueError("Rate-limit identity secret must contain at least 32 bytes.")
        if environment == "production" and self.rate_limit_identity_secret is None:
            raise ValueError("Rate-limit identity secret is required in production.")

        if environment == "production" and self.invitation_email_mode == "console":
            raise ValueError("Console invitation email delivery is not allowed in production.")

        if environment == "production" and self.invitation_email_mode == "provider":
            if self.resend_api_key is None or not self.resend_api_key.get_secret_value().strip():
                raise ValueError("Resend API key is required in production.")
            if self.email_from_address is None or not self.email_from_address.strip():
                raise ValueError("Email from address is required in production.")

        email_keys = parse_encryption_keyring(
            self.email_outbox_encryption_keyring,
            self.email_outbox_encryption_key,
            domain="Email outbox",
        )
        validate_active_key_version(
            email_keys,
            self.email_outbox_active_key_version,
            domain="Email outbox",
            required=environment == "production",
        )

        integration_keys = parse_encryption_keyring(
            self.integration_token_encryption_keyring,
            self.integration_token_encryption_key,
            domain="Integration token",
        )
        validate_active_key_version(
            integration_keys,
            self.integration_token_active_key_version,
            domain="Integration token",
            required=False,
        )

        if self.youtube_upload_chunk_size_bytes % 262_144 != 0:
            raise ValueError("YouTube upload chunk size must be a multiple of 256 KiB.")

        if self.youtube_publish_worker_enabled:
            if not self.youtube_client_id or not self.youtube_client_secret:
                raise ValueError(
                    "YouTube OAuth client credentials are required "
                    "when the publishing worker is enabled."
                )
            if not integration_keys:
                raise ValueError(
                    "Integration encryption keys are required for the publishing worker."
                )
            validate_active_key_version(
                integration_keys,
                self.integration_token_active_key_version,
                domain="Integration token",
                required=True,
            )
            proxy_url = (self.youtube_egress_proxy_url or "").strip()
            parsed_proxy = urlsplit(proxy_url)
            try:
                proxy_port = parsed_proxy.port
            except ValueError as exc:
                raise ValueError("YouTube egress proxy URL has an invalid port.") from exc
            if (
                parsed_proxy.scheme != "http"
                or not parsed_proxy.hostname
                or parsed_proxy.username is not None
                or parsed_proxy.password is not None
                or parsed_proxy.path not in {"", "/"}
                or parsed_proxy.query
                or parsed_proxy.fragment
                or proxy_port != 3128
            ):
                raise ValueError("YouTube publishing requires an HTTP egress proxy on port 3128.")

        if environment == "production":
            if self.youtube_client_id or self.youtube_client_secret:
                redirect = urlsplit(self.youtube_redirect_uri)
                result_url = urlsplit(self.youtube_frontend_result_url)
                if (
                    redirect.scheme != "https"
                    or not redirect.hostname
                    or redirect.username is not None
                    or redirect.password is not None
                    or redirect.query
                    or redirect.fragment
                ):
                    raise ValueError(
                        "YouTube redirect URI must be HTTPS without credentials, query, or "
                        "fragment in production."
                    )
                if (
                    result_url.scheme != "https"
                    or not result_url.hostname
                    or result_url.username is not None
                    or result_url.password is not None
                    or (result_url.scheme, result_url.netloc) != (frontend.scheme, frontend.netloc)
                ):
                    raise ValueError(
                        "YouTube frontend result URL must use the configured frontend HTTPS "
                        "origin in production."
                    )

            if self.youtube_client_id and not self.youtube_client_secret:
                raise ValueError("YouTube client secret is required when OAuth is configured.")
            if self.youtube_client_secret and not self.youtube_client_id:
                raise ValueError("YouTube client ID is required when OAuth is configured.")
            if self.youtube_client_id and not integration_keys:
                raise ValueError(
                    "Integration token encryption key is required when YouTube OAuth is configured."
                )

        if self.story_worker_enabled:
            if not self.story_worker_llm_base_url or not self.story_worker_llm_base_url.strip():
                raise ValueError(
                    "Story worker LLM base URL is required when the worker is enabled."
                )
            if not self.story_worker_model or not self.story_worker_model.strip():
                raise ValueError("Story worker model is required when the worker is enabled.")

        if self.media_worker_enabled:
            required_media_settings = {
                "MEDIA_VIDEO_ENCODER": self.media_video_encoder,
                "MEDIA_TTS_RIGHTS_BASIS": self.media_tts_rights_basis,
            }
            if self.media_tts_provider in {
                "openai_compatible",
                "kokoro",
                "qwen3_tts",
                "openai",
                "custom",
            }:
                required_media_settings.update(
                    {
                        "MEDIA_TTS_BASE_URL": self.media_tts_base_url,
                        "MEDIA_TTS_MODEL": self.media_tts_model,
                        "MEDIA_TTS_VOICE": self.media_tts_voice,
                    }
                )
            elif self.media_tts_provider == "elevenlabs":
                required_media_settings.update(
                    {
                        "MEDIA_TTS_BASE_URL": self.media_tts_base_url,
                        "MEDIA_TTS_MODEL": self.media_tts_model,
                        "MEDIA_TTS_VOICE": self.media_tts_voice,
                    }
                )
                if (
                    self.media_tts_api_key is None
                    or not self.media_tts_api_key.get_secret_value().strip()
                ):
                    raise ValueError("MEDIA_TTS_API_KEY is required for the ElevenLabs provider.")
                if self.media_tts_hosting_mode != "hosted":
                    raise ValueError("ElevenLabs TTS requires MEDIA_TTS_HOSTING_MODE=hosted.")
                if not 0.7 <= self.media_tts_speed <= 1.2:
                    raise ValueError("ElevenLabs TTS speed must be between 0.7 and 1.2.")
            elif self.media_tts_provider == "mock":
                if environment == "production":
                    raise ValueError("The mock TTS provider is not allowed in production.")
                if self.media_tts_rights_basis != "generated_in_application":
                    raise ValueError(
                        "Mock TTS requires MEDIA_TTS_RIGHTS_BASIS=generated_in_application."
                    )
            missing_media_settings = [
                name
                for name, value in required_media_settings.items()
                if value is None or not value.strip()
            ]
            if missing_media_settings:
                raise ValueError(
                    "Media worker is enabled but required settings are missing: "
                    + ", ".join(missing_media_settings)
                )
            if self.media_storage_backend == "local":
                if not self.media_storage_root.is_absolute():
                    raise ValueError("Media storage root must be an absolute path.")
                if (
                    environment == "production"
                    and "media_storage_root" not in self.model_fields_set
                ):
                    raise ValueError(
                        "MEDIA_STORAGE_ROOT must explicitly point to persistent storage "
                        "in production."
                    )
            else:
                required_s3_settings = {
                    "MEDIA_S3_ENDPOINT_URL": self.media_s3_endpoint_url,
                    "MEDIA_S3_BUCKET": self.media_s3_bucket,
                    "MEDIA_S3_ACCESS_KEY_ID": (
                        self.media_s3_access_key_id.get_secret_value()
                        if self.media_s3_access_key_id is not None
                        else None
                    ),
                    "MEDIA_S3_SECRET_ACCESS_KEY": (
                        self.media_s3_secret_access_key.get_secret_value()
                        if self.media_s3_secret_access_key is not None
                        else None
                    ),
                }
                missing_s3_settings = [
                    name
                    for name, value in required_s3_settings.items()
                    if value is None or not value.strip()
                ]
                if missing_s3_settings:
                    raise ValueError(
                        "S3 media storage is selected but required settings are missing: "
                        + ", ".join(missing_s3_settings)
                    )
                if not self.media_s3_region.strip():
                    raise ValueError("MEDIA_S3_REGION must be non-empty.")
            if not self.media_ffmpeg_path.strip() or not self.media_ffprobe_path.strip():
                raise ValueError("Media FFmpeg and ffprobe paths must be non-empty.")
            if not self.media_audio_encoder.strip() or not self.media_video_preset.strip():
                raise ValueError("Media audio encoder and video preset must be non-empty.")
            if self.media_tts_rights_basis in {"licensed", "cc_by"} and not (
                self.media_tts_license_id
                and self.media_tts_license_id.strip()
                and self.media_tts_license_uri
                and self.media_tts_license_uri.strip()
                and self.media_tts_attribution
                and self.media_tts_attribution.strip()
            ):
                raise ValueError(
                    "MEDIA_TTS_LICENSE_ID, MEDIA_TTS_LICENSE_URI and "
                    "MEDIA_TTS_ATTRIBUTION are required for licensed TTS assets."
                )
            if self.media_tts_rights_basis in {"cc0", "licensed", "cc_by"}:
                if not self.media_tts_license_id or not self.media_tts_license_id.strip():
                    raise ValueError("MEDIA_TTS_LICENSE_ID is required for licensed TTS assets.")
                license_uri = urlsplit(self.media_tts_license_uri or "")
                try:
                    license_port = license_uri.port
                except ValueError as exc:
                    raise ValueError("MEDIA_TTS_LICENSE_URI is invalid.") from exc
                if (
                    license_uri.scheme != "https"
                    or not license_uri.hostname
                    or license_uri.username is not None
                    or license_uri.password is not None
                    or license_port not in {None, 443}
                    or license_uri.query
                    or license_uri.fragment
                ):
                    raise ValueError("MEDIA_TTS_LICENSE_URI must be a canonical HTTPS URL.")
            if self.media_tts_rights_basis in {"public_domain", "cc0", "licensed", "cc_by"}:
                source_uri = urlsplit(self.media_tts_source_uri or "")
                try:
                    source_port = source_uri.port
                except ValueError as exc:
                    raise ValueError("MEDIA_TTS_SOURCE_URI is invalid.") from exc
                if (
                    source_uri.scheme != "https"
                    or not source_uri.hostname
                    or source_uri.username is not None
                    or source_uri.password is not None
                    or source_port not in {None, 443}
                    or source_uri.query
                    or source_uri.fragment
                ):
                    raise ValueError("MEDIA_TTS_SOURCE_URI must be a canonical HTTPS URL.")
            if self.media_visual_provider == "wikimedia_commons":
                user_agent = self.media_wikimedia_user_agent or ""
                contact = user_agent.partition("(")[2].rpartition(")")[0].strip()
                has_contact = bool(
                    _WIKIMEDIA_CONTACT_EMAIL.search(contact)
                    or _WIKIMEDIA_CONTACT_URL.search(contact)
                )
                if (
                    not user_agent.startswith("AuroraStudio/")
                    or len(user_agent) > 256
                    or "\n" in user_agent
                    or "\r" in user_agent
                    or not has_contact
                ):
                    raise ValueError(
                        "MEDIA_WIKIMEDIA_USER_AGENT must identify Aurora Studio "
                        "and include contact information."
                    )
            if self.media_visual_provider == "comfyui":
                if not self.media_comfyui_base_url or not self.media_comfyui_base_url.strip():
                    raise ValueError("MEDIA_COMFYUI_BASE_URL is required for the ComfyUI provider.")
                if self.media_comfyui_workflow_path is None:
                    raise ValueError(
                        "MEDIA_COMFYUI_WORKFLOW_PATH is required for the ComfyUI provider."
                    )
                if (
                    not self.media_comfyui_output_node_id
                    or not self.media_comfyui_output_node_id.strip()
                ):
                    raise ValueError(
                        "MEDIA_COMFYUI_OUTPUT_NODE_ID is required for the ComfyUI provider."
                    )

        if self.media_comfyui_base_url is not None:
            comfy_url = urlsplit(self.media_comfyui_base_url)
            try:
                comfy_port = comfy_url.port
            except ValueError as exc:
                raise ValueError("ComfyUI base URL has an invalid port.") from exc
            local_comfy = (
                comfy_url.scheme == "http"
                and comfy_url.hostname == "host.docker.internal"
                and comfy_port == 8188
            )
            local_wsl_comfy = (
                comfy_url.scheme == "http"
                and comfy_url.hostname == "comfyui.wsl.host"
                and comfy_port == 8188
            )
            hosted_comfy = comfy_url.scheme == "https" and comfy_url.hostname is not None
            if (
                not (local_comfy or local_wsl_comfy or hosted_comfy)
                or comfy_url.username is not None
                or comfy_url.password is not None
                or comfy_url.query
                or comfy_url.fragment
            ):
                raise ValueError(
                    "ComfyUI URL must be HTTPS or an explicitly allowed local "
                    "host.docker.internal:8188 or comfyui.wsl.host:8188 endpoint."
                )
        if (
            self.media_comfyui_workflow_path is not None
            and not self.media_comfyui_workflow_path.is_absolute()
        ):
            raise ValueError("MEDIA_COMFYUI_WORKFLOW_PATH must be an absolute path.")

        if self.media_s3_endpoint_url is not None:
            s3_url = urlsplit(self.media_s3_endpoint_url)
            if (
                s3_url.scheme not in {"http", "https"}
                or not s3_url.hostname
                or s3_url.username is not None
                or s3_url.password is not None
                or s3_url.query
                or s3_url.fragment
                or s3_url.path not in {"", "/"}
            ):
                raise ValueError(
                    "Media S3 endpoint must be a valid HTTP(S) origin without credentials."
                )
            if environment == "production" and s3_url.scheme != "https":
                raise ValueError("Media S3 endpoint must use HTTPS in production.")

        if self.media_tts_base_url is not None:
            tts_url = urlsplit(self.media_tts_base_url)
            if (
                tts_url.scheme not in {"http", "https"}
                or not tts_url.hostname
                or tts_url.username is not None
                or tts_url.password is not None
                or tts_url.query
                or tts_url.fragment
            ):
                raise ValueError("Media TTS base URL must be a valid HTTP(S) origin/path.")
            if environment == "production" and tts_url.scheme != "https":
                raise ValueError("Media TTS base URL must use HTTPS in production.")

        if self.media_tts_proxy_url is not None:
            proxy_url = urlsplit(self.media_tts_proxy_url)
            if (
                proxy_url.scheme not in {"http", "https"}
                or not proxy_url.hostname
                or proxy_url.username is not None
                or proxy_url.password is not None
                or proxy_url.query
                or proxy_url.fragment
                or proxy_url.path not in {"", "/"}
            ):
                raise ValueError("Media TTS proxy URL must be a valid HTTP(S) origin.")

        if self.media_visual_proxy_url is not None:
            proxy_url = urlsplit(self.media_visual_proxy_url)
            if (
                proxy_url.scheme not in {"http", "https"}
                or not proxy_url.hostname
                or proxy_url.username is not None
                or proxy_url.password is not None
                or proxy_url.query
                or proxy_url.fragment
                or proxy_url.path not in {"", "/"}
            ):
                raise ValueError("Media visual proxy URL must be a valid HTTP(S) origin.")

        if self.media_s3_proxy_url is not None:
            proxy_url = urlsplit(self.media_s3_proxy_url)
            if (
                proxy_url.scheme != "http"
                or not proxy_url.hostname
                or proxy_url.username is not None
                or proxy_url.password is not None
                or proxy_url.query
                or proxy_url.fragment
                or proxy_url.path not in {"", "/"}
                or proxy_url.port != 3128
            ):
                raise ValueError("Media S3 proxy URL must be an HTTP origin on port 3128.")

        if self.story_worker_llm_base_url is not None:
            llm_url = urlsplit(self.story_worker_llm_base_url)
            if (
                llm_url.scheme not in {"http", "https"}
                or not llm_url.hostname
                or llm_url.username is not None
                or llm_url.password is not None
                or llm_url.query
                or llm_url.fragment
            ):
                raise ValueError("Story worker LLM base URL must be a valid HTTP(S) origin/path.")

            if environment == "production" and llm_url.scheme != "https":
                raise ValueError("Story worker LLM base URL must use HTTPS in production.")

            endpoint = _STORY_PROVIDER_ENDPOINTS.get(self.story_worker_provider)
            if endpoint and (
                llm_url.scheme != "https"
                or llm_url.hostname != endpoint[0]
                or llm_url.path.rstrip("/") != endpoint[1]
            ):
                raise ValueError(
                    f"{self.story_worker_provider} requires its canonical HTTPS API endpoint."
                )

        if (
            self.story_worker_enabled
            and self.story_worker_provider not in {"openai_compatible", "ollama"}
            and (
                self.story_worker_llm_api_key is None
                or not self.story_worker_llm_api_key.get_secret_value().strip()
            )
        ):
            raise ValueError("The selected hosted story provider requires an API key.")

        if self.email_outbox_delivery_timeout_seconds >= self.email_outbox_lease_seconds:
            raise ValueError(
                "Email delivery timeout must be shorter than the outbox lease duration."
            )

        if self.research_worker_heartbeat_interval_seconds >= self.research_worker_lease_seconds:
            raise ValueError(
                "Research worker heartbeat interval must be shorter than its lease duration."
            )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
