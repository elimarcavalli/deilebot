"""AC-0 — TelegramNormalizer produces AUDIO attachment with provider_media_ref for voice/audio."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from deilebot.foundation.envelope import AttachmentKind
from deilebot.providers.telegram.normalizer import TelegramNormalizer


_NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


def _base_msg(**kwargs):
    defaults = dict(
        message_id=1,
        chat=SimpleNamespace(id=100, type="private", title=None, username="alice"),
        from_user=SimpleNamespace(id=100, full_name="Alice", is_bot=False, username="alice"),
        text=None,
        caption=None,
        date=_NOW,
        photo=None,
        document=None,
        voice=None,
        audio=None,
        reply_to_message=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestNormalizerAudio:
    def test_voice_produces_audio_attachment_with_media_ref(self):
        """AC-0: voice message → AUDIO attachment with provider_media_ref = file_id."""
        voice = SimpleNamespace(
            file_id="AgACAgIAAyEGAQAB",
            mime_type="audio/ogg",
            file_size=12345,
        )
        msg = _base_msg(voice=voice)
        env = TelegramNormalizer().to_envelope(msg)

        assert len(env.attachments) == 1
        att = env.attachments[0]
        assert att.kind == AttachmentKind.AUDIO
        assert att.provider_media_ref == "AgACAgIAAyEGAQAB", (
            "normalizer must store file_id in provider_media_ref"
        )
        assert att.mime == "audio/ogg"
        assert att.size_bytes == 12345

    def test_voice_mime_defaults_to_ogg_when_absent(self):
        """Voice without mime_type defaults to audio/ogg."""
        voice = SimpleNamespace(file_id="file_123", mime_type=None, file_size=None)
        msg = _base_msg(voice=voice)
        env = TelegramNormalizer().to_envelope(msg)

        att = env.attachments[0]
        assert att.mime == "audio/ogg"

    def test_audio_file_produces_audio_attachment_with_media_ref(self):
        """AC-0: audio file message → AUDIO attachment with provider_media_ref = file_id."""
        audio = SimpleNamespace(
            file_id="BQACAgIAAyEGAQAB",
            mime_type="audio/mpeg",
            file_name="song.mp3",
            file_size=500000,
        )
        msg = _base_msg(audio=audio)
        env = TelegramNormalizer().to_envelope(msg)

        assert len(env.attachments) == 1
        att = env.attachments[0]
        assert att.kind == AttachmentKind.AUDIO
        assert att.provider_media_ref == "BQACAgIAAyEGAQAB"
        assert att.mime == "audio/mpeg"
        assert att.filename == "song.mp3"
        assert att.size_bytes == 500000

    def test_audio_mime_defaults_to_mpeg_when_absent(self):
        """Audio file without mime_type defaults to audio/mpeg."""
        audio = SimpleNamespace(file_id="f", mime_type=None, file_name=None, file_size=None)
        msg = _base_msg(audio=audio)
        att = TelegramNormalizer().to_envelope(msg).attachments[0]
        assert att.mime == "audio/mpeg"

    def test_voice_url_is_none_before_resolution(self):
        """URL is None at normalization time — must be resolved by the adapter."""
        voice = SimpleNamespace(file_id="abc", mime_type="audio/ogg", file_size=100)
        msg = _base_msg(voice=voice)
        att = TelegramNormalizer().to_envelope(msg).attachments[0]
        assert att.url is None

    def test_no_audio_attachment_when_text_only(self):
        """Text-only message has no attachments."""
        msg = _base_msg(text="hello")
        env = TelegramNormalizer().to_envelope(msg)
        assert env.attachments == ()
