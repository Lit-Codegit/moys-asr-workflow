"""faster-whisper 本地引擎测试：items 映射 / 转写封装 / 切句 / CLI 退出契约。

模板：tests/test_soniox.py（unittest + mock.patch，不碰真实网络与 GPU）。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from maw import faster_whisper as fw


def _fake_word(text: str, start: float, end: float) -> SimpleNamespace:
    return SimpleNamespace(word=text, start=start, end=end)


def _fake_segment(text: str, start: float, end: float,
                  words: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    return SimpleNamespace(text=text, start=start, end=end, words=words)


class ItemsMappingTests(unittest.TestCase):
    def test_words_to_items_milliseconds(self):
        seg = _fake_segment("你好世界", 0.0, 2.0, words=[
            _fake_word("你", 0.0, 0.5),
            _fake_word("好", 0.5, 1.0),
            _fake_word("世", 1.2, 1.5),
            _fake_word("界", 1.5, 2.0),
        ])
        items = fw.segments_to_items([seg])
        self.assertEqual([it["text"] for it in items], ["你", "好", "世", "界"])  # CJK 无空格
        self.assertEqual(items[0]["start"], 0)
        self.assertEqual(items[0]["end"], 500)
        self.assertEqual(items[2]["start"], 1200)

    def test_western_words_get_leading_spaces(self):
        seg = _fake_segment("hello world", 0.0, 2.0, words=[
            _fake_word("hello", 0.0, 1.0),
            _fake_word("world", 1.0, 2.0),
        ])
        items = fw.segments_to_items([seg])
        self.assertEqual([it["text"] for it in items], ["hello", " world"])  # 空格语言补前导空格
        self.assertEqual("".join(it["text"] for it in items), "hello world")

    def test_segment_without_words_falls_back_to_segment_boundary(self):
        seg = _fake_segment("无词级时间戳", 1.25, 3.75, words=None)
        items = fw.segments_to_items([seg])
        self.assertEqual(items, [{"text": "无词级时间戳", "start": 1250, "end": 3750}])

    def test_zero_length_words_skipped(self):
        seg = _fake_segment("x", 0.0, 1.0, words=[
            _fake_word("a", 0.5, 0.5),   # 零宽 → 跳过
            _fake_word("b", 0.5, 1.0),
        ])
        items = fw.segments_to_items([seg])
        self.assertEqual([it["text"] for it in items], ["b"])


class TranscribeTests(unittest.TestCase):
    def _mock_model(self):
        model = mock.Mock()
        seg = _fake_segment("hello world", 0.0, 2.0, words=[
            _fake_word("hello", 0.0, 1.0),
            _fake_word("world", 1.0, 2.0),
        ])
        info = SimpleNamespace(language="en", duration=2.0)
        model.transcribe.return_value = ([seg], info)
        return model

    def test_transcribe_maps_segments_and_language(self):
        model = self._mock_model()
        with mock.patch.object(fw, "_load_model", return_value=model):
            result = fw.transcribe("a.wav", fw.load_config(),
                                   language="en", on_status=lambda _s: None)
        self.assertEqual(result["language"], "en")
        self.assertEqual(result["text"], "hello world")
        self.assertEqual(result["items"][0]["start"], 0)
        self.assertEqual(result["items"][1]["end"], 2000)
        model.transcribe.assert_called_once()
        kwargs = model.transcribe.call_args.kwargs
        self.assertIs(kwargs["word_timestamps"], True)

    def test_transcribe_speaker_warns_and_ignores(self):
        model = self._mock_model()
        messages: list[str] = []
        with mock.patch.object(fw, "_load_model", return_value=model):
            fw.transcribe("a.wav", fw.load_config(), enable_speaker=True,
                          on_status=messages.append)
        self.assertTrue(any("说话人分离" in m for m in messages))

    def test_transcribe_no_language_passes_none(self):
        model = self._mock_model()
        with mock.patch.object(fw, "_load_model", return_value=model):
            fw.transcribe("a.wav", fw.load_config(), on_status=lambda _s: None)
        self.assertIsNone(model.transcribe.call_args.kwargs.get("language"))


class BuildSegmentsTests(unittest.TestCase):
    def test_build_segments_splits_items(self):
        items = [
            {"text": "你", "start": 0, "end": 300},
            {"text": "好", "start": 300, "end": 600},
            {"text": "吗", "start": 600, "end": 900},
        ]
        segments = fw.build_segments(items, max_len=21, min_len=5, gap_split_ms=1500)
        self.assertTrue(segments)
        self.assertEqual(segments[0]["text"], "你好吗")
        self.assertEqual(segments[0]["start"], 0)
        self.assertEqual(segments[0]["end"], 900)


class ConfigTests(unittest.TestCase):
    def test_load_config_defaults(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            pass
        cfg = fw.load_config()
        self.assertEqual(cfg["device"], "auto")
        self.assertIn("model_path", cfg)
        self.assertIn("vad", cfg)


class CliContractTests(unittest.TestCase):
    """CLI 退出契约（镜像 test_qwen.py 的风格）：不存在的输入 → exit 1；正常流程 → SRT + .mosp。"""

    def _make_audio(self, tmp: Path) -> Path:
        wav = tmp / "tone.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-ar", "16000", "-ac", "1", str(wav)],
            capture_output=True, check=True)
        return wav

    def test_missing_input_exits_1(self):
        from generate_subtitle_faster_whisper_api import main
        with mock.patch("sys.argv", ["generate_subtitle_faster_whisper_api.py",
                                     "does-not-exist.wav"]):
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 1)

    def test_full_flow_emits_srt_and_mosp(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = self._make_audio(Path(tmp))
            out_srt = Path(tmp) / "out.srt"
            result = {
                "text": "hello world",
                "language": "en",
                "items": [
                    {"text": "hello", "start": 0, "end": 1000},
                    {"text": " world", "start": 1000, "end": 2000},  # 空格语言带前导空格
                ],
            }
            with mock.patch("generate_subtitle_faster_whisper_api.transcribe",
                            return_value=result) as transcribe:
                with mock.patch("sys.argv", [
                    "generate_subtitle_faster_whisper_api.py", str(wav),
                    "--output", str(out_srt), "--json", "--no-html",
                    "--model", "large-v3",
                ]):
                    from generate_subtitle_faster_whisper_api import main
                    main()
            transcribe.assert_called_once()
            self.assertTrue(out_srt.exists())
            self.assertIn("hello world", out_srt.read_text(encoding="utf-8"))
            mosp = out_srt.with_suffix(".mosp")
            self.assertTrue(mosp.exists())
            data = json.loads(mosp.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "faster-whisper-large-v3")
            self.assertEqual(data["segments"][0]["text"], "hello world")


if __name__ == "__main__":
    unittest.main()
