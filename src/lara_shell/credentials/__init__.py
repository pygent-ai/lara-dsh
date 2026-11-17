from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .store import (
    DEFAULT_API_KEY_ENV,
    KEYRING_SERVICE,
    USER_CREDENTIALS_FILENAME,
    credential_is_configured,
    delete_keyring_credential,
    delete_user_credential,
    ensure_user_lara_root,
    get_keyring_credential,
    list_user_credential_names,
    load_credentials,
    lookup_credential,
    prompt_for_secret,
    read_env_entries,
    set_keyring_credential,
    set_user_credential,
    user_credentials_path,
    write_env_entries,
)

if TYPE_CHECKING:
    from pygent.agent.pygent_v1.runtime.model_configuration import CredentialEnvironment


def lookup_credential_for_root(
    env_name: str, user_lara_root: str | Path
) -> tuple[str | None, str]:
    """Store-backed adapter for the kernel credential lookup protocol."""

    return lookup_credential(env_name, user_lara_root=user_lara_root)


def build_credential_environment(user_lara_root: str | Path) -> "CredentialEnvironment":
    """Compose the kernel credential environment from shell-owned sources."""

    from pygent.agent.pygent_v1.runtime.model_configuration import CredentialEnvironment

    return CredentialEnvironment(
        user_lara_root,
        lookup=lookup_credential_for_root,
        name_lister=list_user_credential_names,
    )


__all__ = [
    "DEFAULT_API_KEY_ENV",
    "KEYRING_SERVICE",
    "USER_CREDENTIALS_FILENAME",
    "build_credential_environment",
    "credential_is_configured",
    "delete_keyring_credential",
    "delete_user_credential",
    "ensure_user_lara_root",
    "get_keyring_credential",
    "list_user_credential_names",
    "load_credentials",
    "lookup_credential",
    "lookup_credential_for_root",
    "prompt_for_secret",
    "read_env_entries",
    "set_keyring_credential",
    "set_user_credential",
    "user_credentials_path",
    "write_env_entries",
]
