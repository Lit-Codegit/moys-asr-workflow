# pyright: reportAny=false, reportOptionalSubscript=false, reportArgumentType=false, reportCallIssue=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""ASS 样式解析/序列化测试（决策 43）：往返保真、容错契约、颜色规范化。"""

from __future__ import annotations

import unittest

from ass_style import (ASS_ALIGN_TO_SSA, SSA_ALIGN_TO_ASS, ass_color_to_rgba,
                       default_style, extract_styles_from_ass,
                       format_style_line, normalize_ass_color,
                       override_color_to_ass, parse_style_line,
                       parse_styles_text)

# 本机真实目录文件（~/.aegisub/catalog/，2026-08-13 抓取）
REAL_DEFAULT_LINE = ("Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,"
                     "&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1")
REAL_CATALOG_LINES = [
    "Style: lika,思源黑体 CN Heavy,50,&H00FFFFFF,&H00FFFFFF,&H193A85F0,&H910E0807,0,0,0,0,100,100,0,0,1,4,5,2,135,135,140,1",
    "Style: 65,思源黑体 CN Heavy,50,&H00FFFFFF,&H00FFFFFF,&H19AA8C19,&H910E0807,0,0,0,0,100,100,0,0,1,4,5,2,135,135,80,1",
    "Style: 吸酱,思源黑体 CN Heavy,70,&H00FFFFFF,&H00FFFFFF,&H19FFA5CD,&H910E0807,0,0,0,0,100,100,1,0,1,4,6,2,0,0,60,1",
    "Style: 广告词,思源宋体 CN Heavy,70,&H00FFFFFF,&H00FFFFFF,&H19AA8C19,&H910E0807,0,-1,0,0,100,100,0,0,1,2,5,2,135,135,140,1",
    "Style: 旁白,思源黑体 CN Heavy,60,&H00A1687B,&H00FFFFFF,&H19FFF9F7,&H910E0807,0,0,0,0,100,100,1,0,1,5,6,7,50,0,60,1",
    "Style: 萝露艾特,思源黑体 CN Heavy,50,&H00FFFFFF,&H00FFFFFF,&H1900008B,&H910E0807,0,0,0,0,100,100,0,0,1,4,5,2,135,135,140,1",
    "Style: yoei,思源黑体 CN Heavy,50,&H00FFFFFF,&H00FFFFFF,&H1982004B,&H910E0807,0,0,0,0,100,100,0,0,1,4,5,2,135,135,140,1",
    "Style: 表情字体,Noto Sans Symbols 2,56,&H00FFFFFF,&H00FFFFFF,&H19000000,&H910E0807,0,0,0,0,100,100,0,0,1,2,4,2,0,0,60,1",
    "Style: 彩色表情,Noto Color Emoji,56,&H00FFFFFF,&H00FFFFFF,&H19000000,&H910E0807,0,0,0,0,100,100,0,0,1,0,0,2,0,0,60,1",
]

SAMPLE_ASS = """[Script Info]
; junk
Title: x
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: broken,only,three,fields
Style: lika,思源黑体 CN Heavy,50,&H00FFFFFF,&H00FFFFFF,&H193A85F0,&H910E0807,0,0,0,0,100,100,0,0,1,4,5,2,135,135,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\move(0,0,100,100)}hello
"""


class StyleLineTests(unittest.TestCase):
    def test_real_catalog_roundtrip(self):
        """本机 管人切片.sty 全部样式：解析→序列化逐字节保真。"""
        for line in REAL_CATALOG_LINES:
            style = parse_style_line(line)
            self.assertIsNotNone(style, line)
            self.assertEqual(format_style_line(style), line)

    def test_real_default_roundtrip(self):
        style = parse_style_line(REAL_DEFAULT_LINE)
        self.assertIsNotNone(style)
        self.assertEqual(format_style_line(style), REAL_DEFAULT_LINE)

    def test_default_style_serializes_to_aegisub_default(self):
        """Aegisub 默认样式 = 规范 Default 行（供旧工程注入与对比）。"""
        self.assertEqual(format_style_line(default_style()), REAL_DEFAULT_LINE)

    def test_field_count_mismatch_is_malformed(self):
        fields = REAL_DEFAULT_LINE.split(",")
        self.assertIsNone(parse_style_line(",".join(fields[:-1])))   # 22 字段
        self.assertIsNone(parse_style_line(",".join(fields + ["x"])))  # 24 字段

    def test_bad_numeric_fields_are_malformed(self):
        fields = REAL_DEFAULT_LINE.split(",")
        for idx, bad in [(2, "notnum"), (11, "1x"), (18, "abc"), (22, "??")]:
            broken = fields[:]
            broken[idx] = bad
            self.assertIsNone(parse_style_line(",".join(broken)), f"field {idx}")

    def test_bad_color_is_malformed(self):
        fields = REAL_DEFAULT_LINE.split(",")
        broken = fields[:]
        broken[3] = "&HZZZZZZ&"
        self.assertIsNone(parse_style_line(",".join(broken)))

    def test_comma_in_name_replaced_with_semicolon(self):
        style = parse_style_line(REAL_DEFAULT_LINE)
        style["name"] = "a,b"
        self.assertIn("a;b", format_style_line(style))

    def test_alignment_passthrough_no_ssa_conversion(self):
        """v4+ Style 行对齐字段原样透传（ASS \\an 编号），不做 SSA 转换。"""
        for align in range(1, 10):
            style = default_style()
            style["alignment"] = align
            self.assertEqual(parse_style_line(format_style_line(style))["alignment"], align)

    def test_italic_true_serializes_as_minus_one(self):
        style = parse_style_line(REAL_CATALOG_LINES[3])  # 广告词 italic=-1
        self.assertTrue(style["italic"])
        self.assertIn(",0,-1,0,0,", format_style_line(style))

    def test_alignment_conversion_tables_match_aegisub(self):
        for ass, ssa in [(1, 1), (2, 2), (3, 3), (4, 9), (5, 10), (6, 11),
                         (7, 5), (8, 6), (9, 7)]:
            self.assertEqual(ASS_ALIGN_TO_SSA[ass], ssa)
            self.assertEqual(SSA_ALIGN_TO_ASS[ssa], ass)


class ColorTests(unittest.TestCase):
    def test_normalize_six_hex_adds_zero_alpha(self):
        self.assertEqual(normalize_ass_color("&HFFFFFF&"), "&H00FFFFFF")
        self.assertEqual(normalize_ass_color("&HFFFFFF"), "&H00FFFFFF")

    def test_normalize_uppercases_lowercase(self):
        self.assertEqual(normalize_ass_color("&h00ffffff"), "&H00FFFFFF")

    def test_normalize_ssa_decimal_legacy(self):
        # 0x01000000 = BGR 00 00 00 + alpha 01（SSA 十进制写法）
        self.assertEqual(normalize_ass_color("16777216"), "&H01000000")

    def test_garbage_colors_are_none(self):
        for bad in ["", "&H12345&", "&HGGGGGG&", "red", "&HFFFFFFF&"]:
            self.assertIsNone(normalize_ass_color(bad), bad)

    def test_override_color_format(self):
        self.assertEqual(override_color_to_ass("&H193A85F0"), "&H3A85F0&")
        self.assertIsNone(override_color_to_ass("junk"))

    def test_rgba_decomposition(self):
        self.assertEqual(ass_color_to_rgba("&H193A85F0"), (0xF0, 0x85, 0x3A, 0x19))


class StylesTextTests(unittest.TestCase):
    def test_catalog_text_tolerates_bom_comments_blank_and_broken(self):
        text = ("﻿; 我的样式目录\n\n" + "\n".join(REAL_CATALOG_LINES) +
                "\nStyle: broken line\n" + "random junk line\n")
        styles, skipped = parse_styles_text(text)
        self.assertEqual(len(styles), len(REAL_CATALOG_LINES))
        self.assertEqual(skipped, 2)

    def test_extract_styles_from_ass(self):
        styles, skipped = extract_styles_from_ass(SAMPLE_ASS)
        self.assertEqual([s["name"] for s in styles], ["Default", "lika"])
        self.assertEqual(skipped, 1)  # broken 行跳过，[Events] 段不解析

    def test_extract_styles_old_header(self):
        text = "[V4 Styles]\n" + REAL_DEFAULT_LINE + "\n"
        styles, skipped = extract_styles_from_ass(text)
        self.assertEqual(len(styles), 1)
        self.assertEqual(skipped, 0)

    def test_extract_styles_missing_section(self):
        styles, skipped = extract_styles_from_ass("[Script Info]\nPlayResX: 1920\n")
        self.assertEqual(styles, [])
        self.assertEqual(skipped, 0)


if __name__ == "__main__":
    unittest.main()
