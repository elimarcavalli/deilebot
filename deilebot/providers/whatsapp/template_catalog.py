"""WhatsApp template catalog — Meta-approved templates loaded from YAML.

A *template* is a pre-approved message body (with placeholders) that the
operator submitted to Meta and that Meta accepted. The Cloud API requires
templates for any send outside the 24h conversation window.

The catalog is a thin Pydantic registry:

    catalog = WhatsAppTemplateCatalog.from_yaml(Path("config/whatsapp_templates.yaml"))
    tpl = catalog.get("appointment_reminder", language="pt_BR")
    components = tpl.build_components(body_params=["Maria", "10:00"])

The catalog **does not** call the Meta API to verify approval status —
unapproved templates will fail at send time with a clear upstream error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class WhatsAppTemplateParam(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    example: Optional[str] = None


class WhatsAppTemplate(BaseModel):
    """One approved template, one language."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    language: str
    category: str  # 'utility' | 'marketing' | 'authentication' | 'service'
    body_params: Tuple[WhatsAppTemplateParam, ...] = Field(default_factory=tuple)
    header_params: Tuple[WhatsAppTemplateParam, ...] = Field(default_factory=tuple)

    @field_validator("category")
    @classmethod
    def _valid_category(cls, v: str) -> str:
        allowed = {"utility", "marketing", "authentication", "service"}
        if v not in allowed:
            raise ValueError(f"category must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("language")
    @classmethod
    def _non_empty_language(cls, v: str) -> str:
        if not v:
            raise ValueError("language must be non-empty")
        return v

    def build_components(
        self,
        *,
        body_params: Sequence[Any] = (),
        header_params: Sequence[Any] = (),
    ) -> List[Dict[str, Any]]:
        """Build the Cloud API ``components`` array.

        Validates that the number of provided params matches the template
        spec; raises ``ValueError`` otherwise so the caller learns at
        catalog-resolution time, not from a Meta 400 response.
        """
        if len(body_params) != len(self.body_params):
            raise ValueError(
                f"template {self.name!r} expects {len(self.body_params)} body params, "
                f"got {len(body_params)}"
            )
        if len(header_params) != len(self.header_params):
            raise ValueError(
                f"template {self.name!r} expects {len(self.header_params)} header params, "
                f"got {len(header_params)}"
            )
        components: List[Dict[str, Any]] = []
        if self.header_params:
            components.append({
                "type": "header",
                "parameters": [{"type": "text", "text": str(p)} for p in header_params],
            })
        if self.body_params:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in body_params],
            })
        return components


class WhatsAppTemplateCatalog:
    """In-memory lookup over (name, language) -> WhatsAppTemplate."""

    def __init__(self, templates: Sequence[WhatsAppTemplate]):
        self._by_key: Dict[Tuple[str, str], WhatsAppTemplate] = {}
        for t in templates:
            key = (t.name, t.language)
            if key in self._by_key:
                raise ValueError(
                    f"duplicate template entry: {t.name!r} / {t.language!r}"
                )
            self._by_key[key] = t

    @classmethod
    def empty(cls) -> "WhatsAppTemplateCatalog":
        return cls([])

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WhatsAppTemplateCatalog":
        items = raw.get("templates") or []
        if not isinstance(items, list):
            raise ValueError("'templates' must be a list")
        templates = [WhatsAppTemplate.model_validate(item) for item in items]
        return cls(templates)

    @classmethod
    def from_yaml(cls, path: Path) -> "WhatsAppTemplateCatalog":
        if not path.exists():
            return cls.empty()
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"{path}: expected a mapping at top level")
        return cls.from_mapping(raw)

    def get(self, name: str, language: str) -> WhatsAppTemplate:
        key = (name, language)
        tpl = self._by_key.get(key)
        if tpl is None:
            raise KeyError(
                f"unknown WhatsApp template: name={name!r} language={language!r}"
            )
        return tpl

    def has(self, name: str, language: str) -> bool:
        return (name, language) in self._by_key

    def __len__(self) -> int:
        return len(self._by_key)

    def names(self) -> List[str]:
        return sorted({n for n, _ in self._by_key.keys()})
