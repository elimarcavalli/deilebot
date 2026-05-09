"""WhatsApp Cloud API settings + capability matrix."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import SecretStr

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]

    SettingsConfigDict = None  # type: ignore[assignment]


from deilebot.foundation.capabilities import ProviderCapabilities
from deilebot.foundation.envelope import AttachmentKind

WHATSAPP_CAPABILITIES = ProviderCapabilities(
    can_edit_message=False,
    can_react=True,
    can_send_dm=True,
    can_threads=False,
    can_polls=False,
    can_inline_keyboards=True,
    can_slash_commands=False,
    can_voice_messages=True,
    can_send_typing=False,
    can_fetch_user_profile=False,
    has_conversation_window=True,
    max_message_chars=4096,
    max_attachments_per_message=1,
    supported_attachment_kinds=frozenset({
        AttachmentKind.IMAGE,
        AttachmentKind.VIDEO,
        AttachmentKind.AUDIO,
        AttachmentKind.FILE,
        AttachmentKind.STICKER,
    }),
)


class WhatsAppSettings(BaseSettings):
    access_token: SecretStr = SecretStr("")
    phone_number_id: str = ""
    business_account_id: str = ""
    verify_token: SecretStr = SecretStr("")
    app_secret: Optional[SecretStr] = None
    api_version: str = "v22.0"
    self_user_id: str = ""
    # Path to the operator's approved-templates catalog. When the file is
    # absent, the adapter starts with an empty catalog and any send_template
    # call will fall through to Meta with no components — useful for
    # zero-param templates registered straight in Business Manager.
    templates_path: Path = Path("./config/whatsapp_templates.yaml")

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_WHATSAPP_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_WHATSAPP_"
            env_file = ".env"
