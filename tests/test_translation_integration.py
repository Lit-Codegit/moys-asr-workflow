"""决策 42 翻译/导出集成测试：export_srt 导出集、译文写回 mosp、serve.py 端点。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import export_srt  # noqa: E402
import translate_subtitle_api as translate_api  # noqa: E402

SERVER_PATH = ROOT / "server-editor" / "serve.py"
SPEC = importlib.util.spec_from_file_location("asr_local_editor_server", SERVER_PATH)
assert SPEC and SPEC.loader
server_editor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server_editor
SPEC.loader.exec_module(server_editor)


def make_project() -> dict:
    return {
        "media": "clip.mp4",
        "translation_language": "zh",
        "segments": [
            {"start": 0, "end": 1000, "text": "hello", "translation": "你好",
             "color": {"name": "red", "value": "#e74c3c", "start": 0, "end": 1000}},
            {"start": 1000, "end": 2000, "text": "world", "translation": "",
             "color_ref": {"name": "red", "headIdx": 0}},
            {"start": 2000, "end": 3000, "text": "plain"},
        ],
    }


class ExportSrtTests(unittest.TestCase):
    def test_effective_color_resolves_head_ref_and_default(self) -> None:
        project = make_project()
        segments = project["segments"]
        self.assertEqual(export_srt.effective_color(segments[0], segments), "red")
        self.assertEqual(export_srt.effective_color(segments[1], segments), "red")
        self.assertIsNone(export_srt.effective_color(segments[2], segments))

    def test_export_set_writes_full_original_translation_and_colors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            written = export_srt.export_srt_set(make_project(), out)
            names = sorted(p.name for p in written)
            # 全量原文 + 全量译文 + red 原文/译文 + default 原文（无色段；无译文不产 default 译文）
            self.assertEqual(names, [
                "subs.srt", "subs_default.srt", "subs_red.srt",
                "subs_zh.srt", "subs_zh_red.srt",
            ])
            translation = (out / "subs_zh.srt").read_text(encoding="utf-8")
            self.assertIn("你好", translation)
            self.assertNotIn("world", translation)  # 无译文段跳过
            red = (out / "subs_red.srt").read_text(encoding="utf-8")
            self.assertIn("hello", red)
            self.assertIn("world", red)
            self.assertNotIn("plain", red)

    def test_export_colors_empty_list_skips_color_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            written = export_srt.export_srt_set(make_project(), out, colors=[])
            names = sorted(p.name for p in written)
            self.assertEqual(names, ["subs.srt", "subs_zh.srt"])

    def test_export_without_translation_omits_translation_files(self) -> None:
        project = make_project()
        for segment in project["segments"]:
            segment["translation"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            written = export_srt.export_srt_set(project, out)
            self.assertFalse(any("zh" in p.name for p in written))


class WriteMospTests(unittest.TestCase):
    def test_write_mosp_sets_segment_translation_and_language(self) -> None:
        project = make_project()
        translated = [{"start": 1000, "end": 2000, "text": "世界", "index": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "subs.mosp"
            translate_api.write_mosp(project, translated, "zh", target)
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["translation_language"], "zh")
            self.assertEqual(saved["segments"][1]["translation"], "世界")
            self.assertEqual(saved["segments"][0]["translation"], "你好")  # 不覆盖已有

    @mock.patch.object(translate_api, "_call_chat",
                       return_value="1. 你好\n2. 世界")
    def test_cli_write_mosp_only_empty(self, _call_chat: mock.Mock) -> None:
        del _call_chat  # 仅打桩返回值
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "subs.mosp"
            project_path.write_text(
                json.dumps(make_project(), ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(sys, "argv", [
                "translate_subtitle_api.py", str(project_path),
                "--write-mosp", "--only-empty", "--target", "zh",
                "--api-key", "sk-test",
            ]):
                translate_api.main()
            saved = json.loads(project_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["translation_language"], "zh")
            # 只翻空缺：批次 = [段 1, 段 2]，编号 1 → 段 1、编号 2 → 段 2；段 0 保留原译文
            self.assertEqual(saved["segments"][0]["translation"], "你好")
            self.assertEqual(saved["segments"][1]["translation"], "你好")
            self.assertEqual(saved["segments"][2]["translation"], "世界")


class ServeEndpointsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.media = self.root / "clip.mp3"
        self.media.write_bytes(b"0123456789")
        self.project_path = self.root / "subs.mosp"
        self.project_path.write_text(
            json.dumps(make_project(), ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _start(self, server) -> str:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def _post(self, base: str, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_export_srt_endpoint_writes_next_to_mosp(self) -> None:
        project = server_editor.load_project(self.project_path, None, None,
                                             no_waveform=True, peaks_per_second=100)
        with server_editor.EditorServer(("127.0.0.1", 0), project) as server:
            base = self._start(server)
            status, body = self._post(base, "/api/export-srt", {"colors": ["red"]})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            names = sorted(Path(p).name for p in body["files"])
            # 决策 43⑧：SRT 与 ASS 同批产出
            self.assertEqual(names, [
                "subs.ass", "subs.srt", "subs_red.ass", "subs_red.srt",
                "subs_zh.ass", "subs_zh.srt", "subs_zh_red.ass", "subs_zh_red.srt",
            ])
            # 全量固定导出 + 勾选 red；default 桶未勾选不产生
            self.assertFalse((self.root / "subs_default.srt").exists())
            self.assertFalse((self.root / "subs_default.ass").exists())
            # .ass 自带 Default 样式（旧工程无 styles → 自动注入）
            self.assertIn("Style: Default",
                          (self.root / "subs_red.ass").read_text(encoding="utf-8"))

    def test_translate_endpoint_writes_back_to_mosp(self) -> None:
        project = server_editor.load_project(self.project_path, None, None,
                                             no_waveform=True, peaks_per_second=100)
        with mock.patch.object(server_editor.translate_api, "resolve_config",
                               return_value={
                                   "base_url": "https://test.invalid/v1",
                                   "model": "test-model", "api_key": "sk-test",
                                   "target": "zh", "batch_size": 10,
                                   "max_retries": 1,
                                   "system_prompt": "Translate the given subtitles.",
                               }), \
             mock.patch.object(server_editor.translate_api, "_call_chat",
                               return_value="1. 世界\n2. 普通"):
            with server_editor.EditorServer(("127.0.0.1", 0), project,
                                            translate_target="zh") as server:
                base = self._start(server)
                status, body = self._post(base, "/api/translate", {"scope": "missing"})
                self.assertEqual(status, 200)
                self.assertTrue(body["ok"])
                self.assertEqual(body["total"], 2)  # 段 1/2 空缺；段 0 已有译文
                job_id = body["jobId"]
                deadline = time.time() + 10
                state: dict = {"done": False, "error": ""}
                while time.time() < deadline and not state["done"] and not state["error"]:
                    with urllib.request.urlopen(
                            f"{base}/api/translate/{job_id}?since=0", timeout=10) as response:
                        state = json.loads(response.read().decode("utf-8"))
                    time.sleep(0.1)
                self.assertEqual(state["error"], "")
                self.assertTrue(state["done"])
                saved = json.loads(self.project_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["translation_language"], "zh")
                self.assertEqual(saved["segments"][0]["translation"], "你好")
                self.assertEqual(saved["segments"][1]["translation"], "世界")
                self.assertEqual(saved["segments"][2]["translation"], "普通")


if __name__ == "__main__":
    unittest.main()
