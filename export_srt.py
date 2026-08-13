# pyright: reportAny=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false

"""mosp → SRT 导出模块（决策 42）。

一份工程（.mosp）按需导出多份 SRT，全部写入工程同目录：
- 全量原文：<stem>.srt（全部段）
- 全量译文：<stem>_<lang>.srt（只含有译文的段，lang 取顶层 translation_language）
- 按颜色拆分：<stem>_<颜色>.srt（原文，无色段归 <stem>_default.srt）
              + <stem>_<lang>_<颜色>.srt（该色段的译文，仅工程有译文时产生）

serve.py 导出端点（/api/export-srt）与 mosp_to_srt.py CLI 共用本模块。
"""
from __future__ import annotations

import json
from pathlib import Path


def effective_color(segment: dict, segments: list[dict]) -> str | None:
    """段的有效颜色名：优先 color_ref.headIdx 指向的 head 名，其次本段 color head。

    颜色区间（head.start/end）只影响范围外段的归属判断——MAW 约定区间内每段
    都带 color_ref，因此按 ref/head 解析即可；不存在的颜色名按无色处理。
    """
    ref = segment.get("color_ref")
    if isinstance(ref, dict):
        head_idx = ref.get("headIdx")
        if isinstance(head_idx, int) and 0 <= head_idx < len(segments):
            head = segments[head_idx]
            head_color = head.get("color") if isinstance(head, dict) else None
            if isinstance(head_color, dict) and head_color.get("name"):
                return head_color["name"]
    head = segment.get("color")
    if isinstance(head, dict) and head.get("name"):
        return head["name"]
    return None


def ms_to_tc(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


def render_srt(entries: list[dict]) -> str:
    """[{start, end, text}] → SRT 文本（同 translate_subtitle_api.render_srt 格式）。"""
    parts = []
    for i, e in enumerate(entries, 1):
        parts.append(f"{i}\n{ms_to_tc(e['start'])} --> {ms_to_tc(e['end'])}\n{e['text']}")
    return "\n\n".join(parts) + "\n"


def write_srt(path: Path, entries: list[dict]) -> Path | None:
    """写一份 SRT；entries 为空返回 None（不产生空文件）。"""
    if not entries:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_srt(entries), encoding="utf-8")
    return path


def load_mosp(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def export_srt_set(
    project: dict,
    out_dir: Path,
    *,
    base_name: str = "subs",
    colors: list[str] | None = None,
) -> list[Path]:
    """导出整组 SRT 到 out_dir，返回实际写入的文件列表。

    colors：None = 全部已用颜色（含无色 default 桶，若有）；[] = 不产生颜色文件。
    全量原文/译文始终导出。
    """
    segments = project.get("segments", [])
    lang = (project.get("translation_language") or "").strip()
    has_translation = any(
        isinstance(s, dict) and s.get("translation", "").strip() for s in segments
    )
    written: list[Path] = []

    # 全量原文
    original = [
        {"start": int(s["start"]), "end": int(s["end"]), "text": s["text"].strip()}
        for s in segments
        if isinstance(s, dict) and s.get("text", "").strip()
    ]
    if original and (p := write_srt(out_dir / f"{base_name}.srt", original)):
        written.append(p)

    # 全量译文（只含有译文的段）
    if has_translation:
        translated = [
            {"start": int(s["start"]), "end": int(s["end"]),
             "text": s["translation"].strip()}
            for s in segments
            if isinstance(s, dict) and s.get("translation", "").strip()
        ]
        if (p := write_srt(out_dir / f"{base_name}_{lang}.srt", translated)):
            written.append(p)

    # 按颜色拆分（colors=None → 全部已用颜色）
    buckets: dict[str, list[dict]] = {}
    bucket_translations: dict[str, list[dict]] = {}
    for s in segments:
        if not isinstance(s, dict) or not s.get("text", "").strip():
            continue
        color = effective_color(s, segments) or "default"
        buckets.setdefault(color, []).append(
            {"start": int(s["start"]), "end": int(s["end"]), "text": s["text"].strip()}
        )
        if has_translation and s.get("translation", "").strip():
            bucket_translations.setdefault(color, []).append(
                {"start": int(s["start"]), "end": int(s["end"]),
                 "text": s["translation"].strip()}
            )
    if colors is None:
        colors = sorted(buckets.keys())
    for color in colors:
        if color not in buckets:
            continue
        if (p := write_srt(out_dir / f"{base_name}_{color}.srt", buckets[color])):
            written.append(p)
        if color in bucket_translations and (
                p := write_srt(out_dir / f"{base_name}_{lang}_{color}.srt",
                               bucket_translations[color])):
            written.append(p)
    return written
