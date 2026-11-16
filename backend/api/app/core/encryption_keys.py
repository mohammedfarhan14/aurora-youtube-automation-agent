import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Encryption keyring contains a duplicate version.")
        result[key] = value
    return result


def parse_encryption_keyring(
    keyring_json: SecretStr | None,
    legacy_key: SecretStr | None,
    *,
    domain: str,
) -> dict[int, str]:
    """Parse a JSON version-to-hex-key map and merge the legacy version-1 key."""
    keys: dict[int, str] = {}
    if keyring_json is not None:
        try:
            raw = json.loads(
                keyring_json.get_secret_value(),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{domain} encryption keyring must be valid JSON.") from exc
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(
                f"{domain} encryption keyring must be a non-empty JSON object."
            )
        for version_text, key in raw.items():
            if not isinstance(version_text, str) or not version_text.isdecimal():
                raise ValueError(f"{domain} encryption keyring versions must be positive integers.")
            version = int(version_text)
            if version < 1 or version > 2_147_483_647 or str(version) != version_text:
                raise ValueError(
                    f"{domain} encryption keyring versions must be canonical positive integers."
                )
            if not isinstance(key, str):
                raise ValueError(f"{domain} encryption keys must be hexadecimal strings.")
            _validate_hex_key(key, domain=domain)
            keys[version] = key

    if legacy_key is not None:
        legacy_value = legacy_key.get_secret_value()
        _validate_hex_key(legacy_value, domain=domain)
        configured_v1 = keys.get(1)
        if configured_v1 is not None and configured_v1 != legacy_value:
            raise ValueError(
                f"{domain} legacy version-1 key conflicts with the configured keyring."
            )
        keys[1] = legacy_value

    return keys


def _validate_hex_key(value: str, *, domain: str) -> None:
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError(
            f"{domain} encryption keys must contain exactly 64 hexadecimal "
            "characters and be valid hexadecimal."
        )


def validate_active_key_version(
    keys: Mapping[int, str],
    active_version: int,
    *,
    domain: str,
    required: bool,
) -> None:
    if not keys:
        if required:
            raise ValueError(f"{domain} encryption keyring is required in production.")
        return
    if active_version not in keys:
        raise ValueError(
            f"{domain} active encryption key version {active_version} is missing from the keyring."
        )
