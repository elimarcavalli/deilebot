"""WhatsAppFormatter — MarkupAST -> WhatsApp text (limited markdown).

Also renders ``InteractiveControls`` to the Cloud API ``interactive``
payload shape (Reply Buttons + List). The output is the dict that goes
under the ``interactive`` key of the send payload — the adapter wraps
``messaging_product`` / ``to`` / ``type`` around it.
"""

from __future__ import annotations

from typing import Any, Dict, List

from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from deilebot.foundation.interactive import (InteractiveButtonRow,
                                              InteractiveControls,
                                              InteractiveList)
from deilebot.foundation.output_formatter import OutputFormatter


class WhatsAppFormatter(OutputFormatter):
    name = "whatsapp"
    max_message_chars = 4096

    # Cloud API interactive limits
    _MAX_BODY_CHARS = 1024
    _MAX_BUTTON_LABEL_CHARS = 20
    _MAX_LIST_BUTTON_LABEL_CHARS = 20
    _MAX_LIST_ITEM_TITLE_CHARS = 24

    def render(self, ast: MarkupAST) -> str:
        if not ast:
            return ""
        out: List[str] = []
        for span in ast:
            out.append(self._render_span(span))
        return "".join(out)

    def _render_span(self, span: MarkupSpan) -> str:
        k, t = span.kind, span.text
        if k == SpanKind.PLAIN:
            return t
        if k == SpanKind.BOLD:
            return f"*{t}*"
        if k == SpanKind.ITALIC:
            return f"_{t}_"
        if k == SpanKind.STRIKE:
            return f"~{t}~"
        if k == SpanKind.CODE_INLINE:
            return f"`{t}`"
        if k == SpanKind.CODE_BLOCK:
            return f"```\n{t}\n```"
        if k == SpanKind.QUOTE:
            return "\n".join(f"> {line}" for line in t.split("\n"))
        if k == SpanKind.LINK:
            url = span.meta.get("url", "") if span.meta else ""
            return f"{t} ({url})" if url else t
        if k == SpanKind.HEADING:
            return f"*{t}*\n"
        if k == SpanKind.BULLET:
            return f"• {t}"
        if k == SpanKind.NUMBERED:
            idx = span.meta.get("index", "1") if span.meta else "1"
            return f"{idx}. {t}"
        if k == SpanKind.LINE_BREAK:
            return "\n"
        return t

    def render_interactive(
        self,
        controls: InteractiveControls,
        body_text: str = "",
    ) -> Dict[str, Any]:
        """Build the Cloud API ``interactive`` payload for a control set.

        Truncates strings to Meta's per-field limits — Cloud API rejects
        the message outright if the text overflows, with no diagnostic
        beyond a 400. Truncating client-side gives the operator a working
        message rather than a silent failure.
        """
        if isinstance(controls, InteractiveButtonRow):
            return self._render_button_row(controls, body_text)
        if isinstance(controls, InteractiveList):
            return self._render_list(controls, body_text)
        raise ValueError(
            f"WhatsApp formatter does not handle {type(controls).__name__}"
        )

    def _render_button_row(
        self, row: InteractiveButtonRow, body_text: str
    ) -> Dict[str, Any]:
        # WhatsApp caps Reply Buttons at 3 per message; trim silently rather
        # than 400 from Meta. The operator will see only the first three.
        buttons = row.buttons[:3]
        button_payload = []
        for b in buttons:
            if not b.callback_data:
                # WhatsApp Reply Buttons do not support URL — skip URL-only
                # buttons; this matches Cloud API behaviour.
                continue
            button_payload.append({
                "type": "reply",
                "reply": {
                    "id": b.callback_data[:256],
                    "title": b.label[: self._MAX_BUTTON_LABEL_CHARS],
                },
            })
        if not button_payload:
            raise ValueError(
                "InteractiveButtonRow has no callback_data buttons "
                "(WhatsApp Reply Buttons require callback_data)"
            )
        return {
            "type": "button",
            "body": {"text": (body_text or "Selecione:")[: self._MAX_BODY_CHARS]},
            "action": {"buttons": button_payload},
        }

    def _render_list(
        self, lst: InteractiveList, body_text: str
    ) -> Dict[str, Any]:
        sections_payload = []
        for sec in lst.sections:
            rows = []
            for it in sec.items:
                if not it.callback_data:
                    continue
                rows.append({
                    "id": it.callback_data[:200],
                    "title": it.label[: self._MAX_LIST_ITEM_TITLE_CHARS],
                })
            if rows:
                sections_payload.append({
                    "title": sec.title[: self._MAX_LIST_ITEM_TITLE_CHARS],
                    "rows": rows,
                })
        if not sections_payload:
            raise ValueError(
                "InteractiveList has no usable rows "
                "(items need callback_data for WhatsApp List)"
            )
        return {
            "type": "list",
            "body": {"text": (body_text or "Selecione:")[: self._MAX_BODY_CHARS]},
            "action": {
                "button": lst.button_label[: self._MAX_LIST_BUTTON_LABEL_CHARS],
                "sections": sections_payload,
            },
        }
