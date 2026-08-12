"""字幕翻译 CLI 测试：srt/.mosp 解析、SRT 渲染、批次翻译、CLI 退出契约。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from translate_subtitle_api import (parse_srt, parse_subtitles, render_srt,
                                    translate_entries)

SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,500
Hello world

2
00:00:04,000 --> 00:00:06,000
This is a test
"""


class ParseTests(unittest.TestCase):
    def test_parse_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.srt"
            p.write_text(SAMPLE_SRT, encoding="utf-8")
            entries = parse_srt(p)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["start"], 1000)
        self.assertEqual(entries[0]["end"], 3500)
        self.assertEqual(entries[0]["text"], "Hello world")

    def test_parse_mosp(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.mosp"
            p.write_text(json.dumps({"segments": [
                {"start": 0, "end": 1000, "text": "你好"},
                {"start": 1000, "end": 2000, "text": "   "},
            ]}), encoding="utf-8")
            entries = parse_subtitles(p)
        self.assertEqual(len(entries), 1)      # 空白段跳过
        self.assertEqual(entries[0]["text"], "你好")

    def test_render_srt_roundtrip(self):
        entries = [{"start": 1000, "end": 3500, "text": "你好世界"}]
        srt = render_srt(entries)
        self.assertIn("00:00:01,000 --> 00:00:03,500", srt)
        self.assertIn("你好世界", srt)
        self.assertEqual(parse_srt(Path("/dev/null")) if False else len(parse_srt(_srt_file(srt))), 1)


def _srt_file(text: str):
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".srt", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    return Path(tmp.name)


class TranslateTests(unittest.TestCase):
    def test_translate_entries_parses_numbered_response(self):
        config = {"batch_size": 20, "system_prompt": "p", "model": "m", "max_retries": 1}
        entries = [
            {"start": 0, "end": 1000, "text": "Hello"},
            {"start": 1000, "end": 2000, "text": "World"},
        ]
        with mock.patch("translate_subtitle_api._call_chat",
                        return_value="1. 你好\n2. 世界") as call:
            out = translate_entries(config, entries)
        call.assert_called_once()
        self.assertEqual(out[0]["text"], "你好")
        self.assertEqual(out[1]["text"], "世界")
        self.assertEqual(out[0]["start"], 0)   # 时间轴保留

    def test_missing_translation_falls_back_to_original(self):
        config = {"batch_size": 20, "system_prompt": "p", "model": "m", "max_retries": 1}
        entries = [{"start": 0, "end": 1000, "text": "Hello"}]
        with mock.patch("translate_subtitle_api._call_chat", return_value="废话不是编号"):
            out = translate_entries(config, entries)
        self.assertEqual(out[0]["text"], "Hello")   # 兜底原文

    def test_batching_splits_by_batch_size(self):
        config = {"batch_size": 2, "system_prompt": "p", "model": "m", "max_retries": 1}
        entries = [{"start": i, "end": i + 1, "text": f"t{i}"} for i in range(5)]
        calls = []
        with mock.patch("translate_subtitle_api._call_chat",
                        side_effect=lambda _c, _t: calls.append(_t) or "1. x\n2. y"):
            translate_entries(config, entries)
        self.assertEqual(len(calls), 3)            # 5 条 → 3 批（2+2+1）


class CliContractTests(unittest.TestCase):
    def test_missing_input_exits_1(self):
        from translate_subtitle_api import main
        with mock.patch("sys.argv", ["translate_subtitle_api.py", "nope.srt"]):
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 1)

    def test_no_api_key_exits_1(self):
        from translate_subtitle_api import main
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.srt"
            p.write_text(SAMPLE_SRT, encoding="utf-8")
            with mock.patch("sys.argv", ["translate_subtitle_api.py", str(p)]):
                with mock.patch.dict("os.environ", {}, clear=True):
                    with self.assertRaises(SystemExit) as raised:
                        main()
                    self.assertEqual(raised.exception.code, 1)

    def test_full_flow_writes_output(self):
        from translate_subtitle_api import main
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.srt"
            p.write_text(SAMPLE_SRT, encoding="utf-8")
            out = Path(tmp) / "out.srt"
            with mock.patch("translate_subtitle_api._call_chat", return_value="1. 你好\n2. 测试"):
                with mock.patch("sys.argv", [
                    "translate_subtitle_api.py", str(p), "-o", str(out),
                    "--api-key", "sk-test", "--base-url", "http://x", "--model", "m",
                ]):
                    main()
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertIn("你好", text)
            self.assertIn("00:00:01,000 --> 00:00:03,500", text)


if __name__ == "__main__":
    unittest.main()
