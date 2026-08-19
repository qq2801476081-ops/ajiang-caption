from datetime import datetime
import threading
import unittest

from app import OVERLAY_CHARACTER_LIMIT, format_history_entry, selected_translation_languages
from engine import (
    caption_payload,
    parse_sensevoice_result,
    RealtimeTranslationDispatcher,
    remove_rollover_overlap,
    translated_caption_payload,
    translation_contains_source_script,
)


class LanguageParsingTests(unittest.TestCase):
    def test_supported_language_tags(self):
        for language in ("zh", "en", "ja", "ko"):
            with self.subTest(language=language):
                detected, text = parse_sensevoice_result(
                    f"<|{language}|><|NEUTRAL|><|Speech|>Hello，世界！"
                )
                self.assertEqual(language, detected)
                self.assertEqual("Hello，世界！", text)

    def test_unknown_and_legacy_alias(self):
        self.assertEqual(("", "bonjour"), parse_sensevoice_result("<|fr|>bonjour"))
        self.assertEqual(("zh", "你好"), parse_sensevoice_result("<|zn|>你好"))


class CaptionPolicyTests(unittest.TestCase):
    def test_foreign_script_residue_triggers_retry_policy(self):
        self.assertTrue(translation_contains_source_script("ja", "本日から販売します。"))
        self.assertTrue(translation_contains_source_script("ko", "今天부터销售。"))
        self.assertFalse(translation_contains_source_script("ja", "本产品今天开始销售。"))

    class FakeTranslator:
        def translate(self, language, text):
            return {"Hello": "你好"}.get(text, "")

    def test_translated_partial_never_displays_english(self):
        payload = translated_caption_payload(
            self.FakeTranslator(), {"en"}, "Hello", "en", partial=True
        )
        self.assertIsNotNone(payload)
        self.assertEqual("你好", payload["text"])
        self.assertNotIn("Hello", payload["text"])

    def test_translation_pending_does_not_fall_back_to_source(self):
        payload = translated_caption_payload(
            self.FakeTranslator(), {"en"}, "Still translating", "en", partial=True
        )
        self.assertIsNone(payload)

    def test_overlay_page_limit_is_thirty_characters(self):
        self.assertEqual(30, OVERLAY_CHARACTER_LIMIT)

    def test_realtime_dispatcher_drops_queued_stale_partials(self):
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        outputs = []

        class BlockingTranslator:
            def translate(self, language, text):
                if text == "one":
                    started.set()
                    release.wait(2)
                return {"one": "一", "two": "二", "three": "三"}[text]

        def capture(message_type, **payload):
            outputs.append(payload["source_text"])
            if len(outputs) == 2:
                completed.set()

        dispatcher = RealtimeTranslationDispatcher(
            BlockingTranslator(), {"en"}, emit_callback=capture
        )
        dispatcher.submit("one", "en", partial=True)
        self.assertTrue(started.wait(1))
        dispatcher.submit("two", "en", partial=True)
        dispatcher.submit("three", "en", partial=True)
        release.set()
        self.assertTrue(completed.wait(2))
        self.assertEqual(["one", "three"], outputs)

    def test_master_and_per_language_switches(self):
        states = {"en": True, "ja": False, "ko": True}
        self.assertEqual(set(), selected_translation_languages(False, states))
        self.assertEqual({"en", "ko"}, selected_translation_languages(True, states))

    def test_translated_caption_uses_chinese(self):
        payload = caption_payload("Hello", "en", "你好")
        self.assertEqual("你好", payload["text"])
        self.assertEqual("Hello", payload["source_text"])
        self.assertEqual("你好", payload["translation_text"])

    def test_untranslated_caption_preserves_original(self):
        payload = caption_payload("こんにちは", "ja", partial=True)
        self.assertEqual("こんにちは", payload["text"])
        self.assertTrue(payload["partial"])

    def test_rollover_overlap_is_preserved(self):
        self.assertEqual("直播", remove_rollover_overlap("欢迎来到", "来到直播"))


class HistoryTests(unittest.TestCase):
    def test_history_includes_language_source_and_translation(self):
        text = format_history_entry(
            {
                "timestamp": datetime(2026, 8, 11, 13, 22, 10),
                "language": "en",
                "source_text": "Hello everyone.",
                "translation_text": "大家好。",
            }
        )
        self.assertEqual("[13:22:10] [英语] Hello everyone.\n中文：大家好。", text)


if __name__ == "__main__":
    unittest.main()
