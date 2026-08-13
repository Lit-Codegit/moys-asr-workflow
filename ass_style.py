# pyright: reportAny=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false

"""ASS 样式行解析/序列化（决策 43）。

逻辑移植自 Aegisub `src/ass_style.cpp`（AssStyle 构造函数 / UpdateData）
与 `libaegisub/common/color.cpp`（Color 解析与 ASS 格式化），BSD-3-Clause，
署名见 THIRD_PARTY_NOTICES.md。移植范围刻意收窄到 kirinuki 需要的行为：

- 只解析/序列化 **v4+ Style 行**（23 字段）；SSA 旧格式（version=0）不在范围，
  对齐字段因此**原样透传**（v4+ Style 行本身就是 \\an 编号，无需转换）。
- 字段模型 = Aegisub AssStyle 的 JSON 镜像（snake_case），颜色一律规范化成
  `&H{A}{B}{G}{R}` 8 位十六进制大写文本（Aegisub 样式段格式，无尾部 `&`）。
- 容错契约（决策 43⑤）：字段数不是 23、数值/颜色解析失败 → 整行视为损坏，
  返回 None，由调用方跳过并计数。与 Aegisub 解析器同样严格。
"""

from __future__ import annotations

import re

# Style 行的字段顺序（ASS [V4+ Styles] 规范顺序）
STYLE_FIELDS = [
    "name", "font", "font_size",
    "primary", "secondary", "outline", "shadow",
    "bold", "italic", "underline", "strikeout",
    "scale_x", "scale_y", "spacing", "angle",
    "border_style", "outline_w", "shadow_w", "alignment",
    "margin_l", "margin_r", "margin_v", "encoding",
]

# Aegisub AssStyle 默认值（ass_style.h 成员初始化 + 构造函数 Margin=10）
_STYLE_DEFAULTS = {
    "name": "Default",
    "font": "Arial",
    "font_size": 48.0,
    "primary": "&H00FFFFFF",
    "secondary": "&H000000FF",
    "outline": "&H00000000",
    "shadow": "&H00000000",
    "bold": False,
    "italic": False,
    "underline": False,
    "strikeout": False,
    "scale_x": 100.0,
    "scale_y": 100.0,
    "spacing": 0.0,
    "angle": 0.0,
    "border_style": 1,
    "outline_w": 2.0,
    "shadow_w": 2.0,
    "alignment": 2,
    "margin_l": 10,
    "margin_r": 10,
    "margin_v": 10,
    "encoding": 1,
}

# 颜色接受：&H + 6 或 8 位十六进制 + 可选尾部 &（ASS 覆盖/样式两种写法；
# 8 位 = 前两位 alpha + 后六位 BGR），或 SSA 遗留十进制数（Aegisub 同款宽容度）。
# 规范化输出恒为 8 位大写 `&H{A}{B}{G}{R}`。
_COLOR_RE = re.compile(r"&[hH]([0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)&?")

# SSA/ASS 对齐编号互转（ass_style.cpp AssToSsa / SsaToAss；default → 2）
ASS_ALIGN_TO_SSA = {1: 1, 2: 2, 3: 3, 4: 9, 5: 10, 6: 11, 7: 5, 8: 6, 9: 7}
SSA_ALIGN_TO_ASS = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}


def default_style() -> dict:
    """返回 Aegisub 默认样式（新拷贝，调用方可安全修改）。"""
    return dict(_STYLE_DEFAULTS)


def normalize_ass_color(color: object) -> str | None:
    """把任意受支持的 ASS 颜色写法规范化为 `&H{A}{B}{G}{R}` 8 位大写。

    不可解析返回 None。8 位输入原样规范化大小写；6 位输入 alpha 补 00。
    """
    if not isinstance(color, str):
        return None
    text = color.strip()
    m = _COLOR_RE.fullmatch(text)
    if m:
        hex_value = m.group(1)
        if len(hex_value) == 6:
            a, bgr = 0, int(hex_value, 16)
        else:  # 8 位：前两位 alpha，后六位 BGR
            a, bgr = int(hex_value[:2], 16), int(hex_value[2:], 16)
        return "&H%02X%02X%02X%02X" % (a, (bgr >> 16) & 0xFF, (bgr >> 8) & 0xFF, bgr & 0xFF)
    if text.isdigit():  # SSA 遗留十进制（BGR，高位溢出即 alpha）
        value = int(text)
        a = (value >> 24) & 0xFF
        return "&H%02X%02X%02X%02X" % (a, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    return None


def override_color_to_ass(color: str) -> str | None:
    """样式段颜色（8 位 ABGR）→ 覆盖标签写法 `&H{B}{G}{R}&`（6 位 + 尾部 &）。

    与 Aegisub `Color::GetAssOverrideFormatted` 一致。非样式段颜色返回 None。
    """
    normalized = normalize_ass_color(color)
    if not normalized:
        return None
    return "&H%s&" % normalized[4:10]  # 去掉 &H + alpha 两位


def ass_color_to_rgba(color: str) -> tuple[int, int, int, int] | None:
    """`&H{A}{B}{G}{R}` → (r, g, b, a)，供编辑器 UI 颜色选择器使用。"""
    normalized = normalize_ass_color(color)
    if not normalized:
        return None
    return (int(normalized[8:10], 16), int(normalized[6:8], 16),
            int(normalized[4:6], 16), int(normalized[2:4], 16))


def _clamp_margin(value: int) -> int:
    """Aegisub 边距钳制 mid(-9999, x, 99999)。"""
    return max(-9999, min(99999, value))


def parse_style_line(line: object) -> dict | None:
    """解析一条 v4+ Style 行（可带 `Style:` 前缀；Aegisub 取首个冒号之后）。

    成功返回 23 字段 dict（颜色已规范化）；失败返回 None。多字段（>23）
    同样视为损坏——与 Aegisub `parser::check_done` 行为一致。
    """
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text:
        return None
    if ":" in text:
        text = text.split(":", 1)[1]
    fields = [f.strip() for f in text.split(",")]
    if len(fields) != len(STYLE_FIELDS):
        return None
    try:
        primary = normalize_ass_color(fields[3])
        secondary = normalize_ass_color(fields[4])
        outline = normalize_ass_color(fields[5])
        shadow = normalize_ass_color(fields[6])
        if not (primary and secondary and outline and shadow):
            return None
        return {
            "name": fields[0],
            "font": fields[1],
            "font_size": float(fields[2]),
            "primary": primary,
            "secondary": secondary,
            "outline": outline,
            "shadow": shadow,
            "bold": int(fields[7]) != 0,
            "italic": int(fields[8]) != 0,
            "underline": int(fields[9]) != 0,
            "strikeout": int(fields[10]) != 0,
            "scale_x": float(fields[11]),
            "scale_y": float(fields[12]),
            "spacing": float(fields[13]),
            "angle": float(fields[14]),
            "border_style": int(fields[15]),
            "outline_w": float(fields[16]),
            "shadow_w": float(fields[17]),
            "alignment": int(fields[18]),
            "margin_l": _clamp_margin(int(fields[19])),
            "margin_r": _clamp_margin(int(fields[20])),
            "margin_v": _clamp_margin(int(fields[21])),
            "encoding": int(fields[22]),
        }
    except ValueError:
        return None


def format_style_line(style: dict) -> str:
    """样式 dict → `Style:` 行。与 Aegisub `AssStyle::UpdateData` 输出一致：

    - name/font 中逗号替换为分号（Aegisub 同款防破坏定界符）
    - 布尔 true 序列化为 -1（SSA 遗留惯例）
    - 浮点用 `%g`（整数不带小数点），颜色 8 位大写
    - 缺失字段按 Aegisub 默认值补齐（旧工程/手工 JSON 容错）
    """
    merged = dict(_STYLE_DEFAULTS)
    merged.update(style)
    return ("Style: %s,%s,%g,%s,%s,%s,%s,%d,%d,%d,%d,%g,%g,%g,%g,%d,%g,%g,%i,%i,%i,%i,%i"
            % (
                str(merged["name"]).replace(",", ";"),
                str(merged["font"]).replace(",", ";"),
                float(merged["font_size"]),
                normalize_ass_color(merged["primary"]) or _STYLE_DEFAULTS["primary"],
                normalize_ass_color(merged["secondary"]) or _STYLE_DEFAULTS["secondary"],
                normalize_ass_color(merged["outline"]) or _STYLE_DEFAULTS["outline"],
                normalize_ass_color(merged["shadow"]) or _STYLE_DEFAULTS["shadow"],
                -1 if merged["bold"] else 0,
                -1 if merged["italic"] else 0,
                -1 if merged["underline"] else 0,
                -1 if merged["strikeout"] else 0,
                float(merged["scale_x"]),
                float(merged["scale_y"]),
                float(merged["spacing"]),
                float(merged["angle"]),
                int(merged["border_style"]),
                float(merged["outline_w"]),
                float(merged["shadow_w"]),
                int(merged["alignment"]),
                _clamp_margin(int(merged["margin_l"])),
                _clamp_margin(int(merged["margin_r"])),
                _clamp_margin(int(merged["margin_v"])),
                int(merged["encoding"]),
            ))


def parse_styles_text(text: str) -> tuple[list[dict], int]:
    """解析 .sty 目录文件全文（可含 BOM/空行/注释行）。

    返回 (样式列表, 跳过行数)。跳过 = 非空、非 `;` 注释但解析失败的行；
    失败行不阻断其余样式（决策 43⑤ 容错契约）。
    """
    styles: list[dict] = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.lstrip("﻿").strip()
        if not line or line.startswith(";"):
            continue
        style = parse_style_line(line)
        if style is None:
            skipped += 1
        else:
            styles.append(style)
    return styles, skipped


def extract_styles_from_ass(text: str) -> tuple[list[dict], int]:
    """从 .ass 文件提取 `[V4+ Styles]`（含 `[V4 Styles]` 旧写法）段的样式。

    其余段（[Script Info]/[Events] 等）整体跳过——v1 不解析对话行（决策 43⑤）。
    返回 (样式列表, 跳过行数)。找不到样式段返回 ([], 0)。
    """
    lines = text.replace("﻿", "").splitlines()
    in_styles = False
    section_text: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_styles = bool(re.match(r"\[V4\+?\s*Styles\]", stripped, re.IGNORECASE))
            continue
        if in_styles and stripped and not stripped.lower().startswith("format:"):
            section_text.append(stripped)
    return parse_styles_text("\n".join(section_text))
