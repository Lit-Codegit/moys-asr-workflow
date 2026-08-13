# pyright: reportAny=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""ASS 导出集测试（决策 43）：样式解析链、覆盖标签、分色/译文、PlayRes。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ass_export import (DEFAULT_PLAYRES, export_ass_set, probe_media_resolution,
                        render_ass, resolve_style_name, segment_overrides_text)
from ass_style import default_style, format_style_line, parse_style_line
from mosp_to_srt import main as mosp_to_srt_main

LIKA_LINE = ("Style: lika,思源黑体 CN Heavy,50,&H00FFFFFF,&H00FFFFFF,&H193A85F0,"
             "&H910E0807,0,0,0,0,100,100,0,0,1,4,5,2,135,135,140,1")
LIKA_STYLE = parse_style_line(LIKA_LINE)


def _seg(start, end, text, **extra):
    seg = {"start": start, "end": end, "text": text}
    seg.update(extra)
    return seg


def _project(segments, **extra):
    project = {"media": "", "language": "Chinese", "segments": segments}
    project.update(extra)
    return project


def _red_segments():
    """红蓝两段：红用 color head + 同色 ref，蓝用 head；无色一段。"""
    return [
        _seg(0, 1000, "红的", color={"name": "red", "value": "#e74c3c"}),
        _seg(1000, 2000, "还是红", color_ref={"name": "red", "headIdx": 0}),
        _seg(2000, 3000, "蓝的", color={"name": "blue", "value": "#3498db"}),
        _seg(3000, 4000, "无色", color=None),
    ]


class ResolveChainTests(unittest.TestCase):
    def setUp(self):
        self.styles = [default_style(), dict(LIKA_STYLE)]
        self.names = {s["name"] for s in self.styles}
        self.color_styles = {"red": "lika"}

    def test_uncolored_segment_gets_default(self):
        seg = _seg(0, 100, "x")
        self.assertEqual(
            resolve_style_name(seg, [seg], self.names, self.color_styles), "Default")

    def test_color_binding_resolves(self):
        segs = [_seg(0, 100, "head", color={"name": "red"}),
                _seg(100, 200, "ref", color_ref={"name": "red", "headIdx": 0})]
        self.assertEqual(
            resolve_style_name(segs[1], segs, self.names, self.color_styles), "lika")

    def test_segment_style_ref_wins_over_color(self):
        segs = [_seg(0, 100, "head", color={"name": "red"})]
        seg = _seg(100, 200, "直选", color_ref={"name": "red", "headIdx": 0},
                   style_ref="lika")
        self.assertEqual(
            resolve_style_name(seg, segs, self.names, self.color_styles), "lika")

    def test_missing_style_ref_falls_through_to_color(self):
        segs = [_seg(0, 100, "head", color={"name": "red"})]
        seg = _seg(100, 200, "x", color_ref={"name": "red", "headIdx": 0},
                   style_ref="不存在")
        self.assertEqual(
            resolve_style_name(seg, segs, self.names, self.color_styles), "lika")

    def test_color_mapping_to_missing_style_falls_back_to_default(self):
        segs = [_seg(0, 100, "head", color={"name": "red"})]
        self.assertEqual(
            resolve_style_name(segs[0], segs, self.names, {"red": "不存在"}), "Default")


class OverrideTagTests(unittest.TestCase):
    def test_full_override_block_order(self):
        """标签按 Aegisub 表序：pos → fad → 3c → fs → b → i。"""
        text = segment_overrides_text({
            "pos": [100, 200], "fade": [300, 400], "font_size": 60,
            "outline": "&H193A85F0", "bold": True, "italic": False,
        })
        self.assertEqual(text, r"{\pos(100,200)\fad(300,400)\3c&H3A85F0&\fs60\b1\i0}")

    def test_empty_and_unknown_overrides(self):
        self.assertEqual(segment_overrides_text({}), "")
        # 未知字段 / 无覆盖标签的样式专属字段（边距/边框样式）被忽略
        self.assertEqual(segment_overrides_text({"margin_l": 999, "border_style": 3}), "")

    def test_malformed_pos_and_fade_are_skipped(self):
        self.assertEqual(segment_overrides_text({"pos": [1], "fade": "x"}), "")

    def test_float_formatting(self):
        text = segment_overrides_text({"font_size": 48.5, "outline_w": 4.0})
        self.assertEqual(text, r"{\bord4\fs48.5}")


class RenderTests(unittest.TestCase):
    def test_legacy_project_injects_default_style(self):
        entries = [{"start": 1234, "end": 5678, "text": "你好", "style_name": "Default",
                    "overrides": {}}]
        text = render_ass(entries, [default_style()], title="subs")
        self.assertIn("[Script Info]", text)
        self.assertIn("ScriptType: v4.00+", text)
        self.assertIn(f"PlayResX: {DEFAULT_PLAYRES[0]}", text)
        self.assertIn("Format: Name, Fontname, Fontsize, PrimaryColour", text)
        self.assertIn(format_style_line(default_style()), text)
        self.assertIn("Dialogue: 0,0:00:01.23,0:00:05.68,Default,,0,0,0,,你好\n", text)

    def test_newline_becomes_soft_line_break(self):
        entries = [{"start": 0, "end": 100, "text": "第一行\n第二行",
                    "style_name": "Default", "overrides": {}}]
        text = render_ass(entries, [default_style()], title="s")
        self.assertIn(r"第一行\N第二行", text)

    def test_overrides_prepend_dialogue_text(self):
        entries = [{"start": 0, "end": 100, "text": "x", "style_name": "lika",
                    "overrides": {"pos": [960, 900]}}]
        text = render_ass(entries, [LIKA_STYLE], title="s")
        self.assertIn(r"{\pos(960,900)}x", text)


class ExportSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_export_set_files_and_style_resolution(self):
        project = _project(_red_segments(), styles=[default_style(), dict(LIKA_STYLE)],
                           color_styles={"red": "lika", "blue": "nope"},
                           translation_language="zh")
        written = sorted(p.name for p in export_ass_set(project, self.out, base_name="subs"))
        # 分色拆分不依赖译文（与 SRT 的 CLI 门控不同；serve.py 按面板选择传 colors）
        self.assertEqual(written, ["subs.ass", "subs_blue.ass", "subs_default.ass",
                                   "subs_red.ass"])

        text = (self.out / "subs.ass").read_text(encoding="utf-8")
        dialogues = [l for l in text.splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), 4)
        self.assertIn(",lika,,0,0,0,,红的\n", text)        # color head 绑定 lika
        self.assertIn(",lika,,0,0,0,,还是红\n", text)       # color_ref 跟随 head
        self.assertIn(",Default,,0,0,0,,蓝的\n", text)      # blue 映射到不存在样式 → Default
        self.assertIn(",Default,,0,0,0,,无色\n", text)

    def test_export_with_translation_and_color_split(self):
        segs = [_seg(0, 1000, "原文", translation="译文",
                     color={"name": "red"}),
                _seg(1000, 2000, "无色段", translation="译二", color=None)]
        project = _project(segs, styles=[default_style(), dict(LIKA_STYLE)],
                           color_styles={"red": "lika"}, translation_language="zh")
        written = sorted(p.name for p in export_ass_set(project, self.out, base_name="subs"))
        self.assertEqual(written, ["subs.ass", "subs_default.ass", "subs_red.ass",
                                   "subs_zh.ass", "subs_zh_default.ass",
                                   "subs_zh_red.ass"])
        red = (self.out / "subs_red.ass").read_text(encoding="utf-8")
        self.assertIn(",lika,,0,0,0,,原文\n", red)
        self.assertNotIn("无色段", red)
        # 每份自带完整 Styles 段（含未使用的 Default/lika）
        self.assertEqual(len([l for l in red.splitlines()
                              if l.startswith("Style: ")]), 2)

    def test_empty_project_writes_nothing(self):
        self.assertEqual(export_ass_set(_project([]), self.out), [])

    def test_missing_media_falls_back_to_default_playres(self):
        self.assertIsNone(probe_media_resolution("/不存在的路径.mp4"))
        text = render_ass([], [default_style()], title="s")
        self.assertIn(f"PlayResX: {DEFAULT_PLAYRES[0]}", text)
        self.assertIn(f"PlayResY: {DEFAULT_PLAYRES[1]}", text)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "需要 ffmpeg/ffprobe")
    def test_probe_real_video_resolution(self):
        video = self.out / "tiny.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=1",
             "-frames:v", "1", "-y", str(video)], check=True, timeout=60)
        self.assertEqual(probe_media_resolution(str(video)), (64, 36))


class CliContractTests(unittest.TestCase):
    """mosp_to_srt.py 同批产出 .ass 的 CLI 契约（决策 43⑧）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.mosp = self.out / "subs.mosp"
        import json
        self.mosp.write_text(json.dumps(_project(_red_segments()),
                                        ensure_ascii=False), encoding="utf-8")

    def _run(self, *argv):
        import sys
        from unittest import mock
        with mock.patch.object(sys, "argv", ["mosp_to_srt.py", *argv]):
            return mosp_to_srt_main()

    def test_default_exports_srt_and_ass(self):
        code = self._run(str(self.mosp), "-o", str(self.out / "subs.srt"))
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "subs.srt").exists())
        self.assertTrue((self.out / "subs.ass").exists())

    def test_translation_colors_exports_full_set(self):
        code = self._run(str(self.mosp), "-o", str(self.out / "subs.srt"),
                         "--translation", "--colors", "all")
        self.assertEqual(code, 0)
        names = sorted(p.name for p in self.out.glob("subs*"))
        # CLI --colors all 与 SRT 同构：只按显式颜色名拆分（无色 default 桶仅
        # 在 colors=None 直接调 export_*_set 时产生——serve.py 面板路径）
        self.assertIn("subs.ass", names)
        self.assertIn("subs_red.ass", names)
        self.assertIn("subs_blue.ass", names)
        self.assertNotIn("subs_default.ass", names)


if __name__ == "__main__":
    unittest.main()
