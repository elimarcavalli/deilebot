"""WhatsApp adapter — webhook-driven (no gateway)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from deilebot.foundation.envelope import (Attachment, BotUser, Channel,
                                           TemplateMessage)
from deilebot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)
from deilebot.foundation.interactive import (InteractiveButtonRow,
                                              InteractiveControls,
                                              InteractiveList)
from deilebot.foundation.logging import get_logger
from deilebot.providers.base import InboundCallback, ProviderAdapter
from deilebot.providers.whatsapp.api_client import WhatsAppApiClient
from deilebot.providers.whatsapp.formatter import WhatsAppFormatter
from deilebot.providers.whatsapp.normalizer import WhatsAppNormalizer
from deilebot.providers.whatsapp.settings import (WHATSAPP_CAPABILITIES,
                                                   WhatsAppSettings)
from deilebot.providers.whatsapp.template_catalog import \
    WhatsAppTemplateCatalog


class WhatsAppAdapter(ProviderAdapter):
    name = "whatsapp"
    capabilities = WHATSAPP_CAPABILITIES

    def __init__(
        self,
        settings: WhatsAppSettings,
        on_inbound: Optional[InboundCallback] = None,
        *,
        runtime: Optional[Any] = None,
        template_catalog: Optional[WhatsAppTemplateCatalog] = None,
    ):
        self.settings = settings
        self.on_inbound = on_inbound
        self.normalizer = WhatsAppNormalizer()
        self._client: Optional[WhatsAppApiClient] = None
        self._self_user_id = settings.self_user_id or settings.phone_number_id
        self._logger = get_logger("whatsapp.adapter")
        self.runtime: Optional[Any] = runtime
        if template_catalog is not None:
            self.template_catalog = template_catalog
        else:
            try:
                self.template_catalog = WhatsAppTemplateCatalog.from_yaml(
                    Path(settings.templates_path)
                )
            except Exception as e:  # noqa: BLE001
                # A malformed YAML must not stop the adapter from booting —
                # operators add templates incrementally; we surface the
                # error in logs and start with an empty catalog so free
                # text inside the 24h window still works.
                self._logger.warning(
                    "WhatsApp template catalog load failed (%s); starting empty",
                    e,
                )
                self.template_catalog = WhatsAppTemplateCatalog.empty()
        self._formatter = WhatsAppFormatter()

    @property
    def self_user_id(self) -> str:
        return self._self_user_id

    async def start(self) -> None:
        token = self.settings.access_token.get_secret_value()
        if not token or not self.settings.phone_number_id:
            raise ProviderError("WhatsApp access_token + phone_number_id required", context={})
        self._client = WhatsAppApiClient(
            token, self.settings.phone_number_id, self.settings.api_version
        )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def handle_webhook(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        env = self.normalizer.to_envelope(payload)
        if env is None or self.on_inbound is None:
            return {"status": "ignored"}
        await self.on_inbound(env, self)
        return {"status": "ok"}

    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        wa_id = channel.provider_channel_id.split(":", 1)[-1]
        return await self._client.send_text(wa_id, text)

    async def send_template(
        self,
        user: BotUser,
        template: TemplateMessage,
    ) -> str:
        """Send a template message via Cloud API.

        ``template`` resolves through the catalog so we validate parameter
        counts client-side before issuing the HTTP call. Templates that are
        not in the catalog are sent with no components — useful for
        zero-param templates the operator has not bothered to register.
        """
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        if not isinstance(template, TemplateMessage):
            raise ProviderError(
                "send_template requires a TemplateMessage",
                context={"got": type(template).__name__},
            )
        components: list = []
        if self.template_catalog.has(template.name, template.language):
            tpl = self.template_catalog.get(template.name, template.language)
            try:
                components = tpl.build_components(
                    body_params=template.body_params,
                    header_params=template.header_params,
                )
            except ValueError as e:
                raise ProviderError(
                    f"WhatsApp template parameter mismatch: {e}",
                    context={"template": template.name, "language": template.language},
                ) from e
        return await self._client.send_template(
            user.provider_user_id,
            template.name,
            template.language,
            components=components,
        )

    async def send_interactive(
        self,
        channel: Channel,
        controls: InteractiveControls,
        body_text: str = "",
        *,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send an Interactive Message (List or Reply Buttons) via Cloud API.

        ``body_text`` is the prompt that appears above the controls. The
        formatter renders ``controls`` to the Cloud API ``interactive``
        payload shape; we just wrap and POST.
        """
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        if not isinstance(controls, (InteractiveList, InteractiveButtonRow)):
            raise CapabilityNotSupported(
                f"WhatsApp does not support interactive type {type(controls).__name__}"
            )
        wa_id = channel.provider_channel_id.split(":", 1)[-1]
        interactive_payload = self._formatter.render_interactive(controls, body_text)
        payload: dict = {
            "messaging_product": "whatsapp",
            "to": wa_id,
            "type": "interactive",
            "interactive": interactive_payload,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._client._post(payload)

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        raise CapabilityNotSupported("WhatsApp does not support edit_message")

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        wa_id = channel.provider_channel_id.split(":", 1)[-1]
        try:
            await self._client._post({
                "messaging_product": "whatsapp",
                "to": wa_id,
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": emoji},
            })
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"whatsapp react failed: {e}", context={}) from e

    async def send_dm(
        self,
        user: BotUser,
        text: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._client is None:
            raise ProviderError("WhatsApp client not started", context={})
        return await self._client.send_text(user.provider_user_id, text)

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        raise CapabilityNotSupported("WhatsApp does not expose user profile")

    async def send_typing(self, channel: Channel) -> None:
        return  # WhatsApp Cloud API does not expose typing
