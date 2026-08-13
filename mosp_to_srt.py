# pyright: reportAny=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false

"""把 MAWE 编辑后的 .mosp 工程文件导出为 SRT（决策 42：多文件导出集）。

默认（无额外参数）：只导出全量原文 SRT（历史行为，供 p3 字幕烧录）。
- `--translation`：追加导出全量译文 <stem>_<lang>.srt（只含有译文的段）
- `--colors all|<颜色名,...>`：按颜色拆分导出 <stem>_<颜色>.srt 与
  <stem>_<lang>_<颜色>.srt（无色段 = default；需要 --translation 才有译文分色文件）

用法：
    python mosp_to_srt.py <input.mosp> [-o output.srt]
    python mosp_to_srt.py subs.mosp -o subs/subs.srt --translation --colors all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from export_srt import export_srt_set, load_mosp


def main() -> int:
    parser = argparse.ArgumentParser(description=".mosp 工程 → SRT（决策 42 导出集）")
    parser.add_argument("input", help="输入 .mosp 工程文件")
    parser.add_argument("-o", "--output", help="全量原文 SRT 输出路径（默认与输入同名 .srt）")
    parser.add_argument("--translation", action="store_true",
                        help="同时导出全量译文 <stem>_<lang>.srt")
    parser.add_argument("--lang", default="",
                        help="译文语言后缀覆盖（默认取工程 translation_language）")
    parser.add_argument("--colors", default="",
                        help='按颜色拆分导出："all" 或逗号分隔颜色名（如 red,blue）')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在 - {input_path}", file=sys.stderr)
        return 1
    project = load_mosp(input_path)
    if args.lang:
        project["translation_language"] = args.lang
    segments = project.get("segments", [])
    if not segments:
        print("错误: 工程中没有字幕段", file=sys.stderr)
        return 2

    # 历史默认行为：只导全量原文（写到 -o 指定路径；颜色/译文文件写 mosp 同目录）
    out_dir = (Path(args.output).parent if args.output
               else input_path.parent).resolve()
    base_name = input_path.stem
    if args.output:
        base_name = Path(args.output).stem

    want_colors: list[str] | None = None
    if args.colors:
        want_colors = (sorted({s["color"]["name"] for s in segments
                               if isinstance(s.get("color"), dict) and s["color"].get("name")})
                       if args.colors == "all" else
                       [c.strip() for c in args.colors.split(",") if c.strip()])
    if not args.translation:
        # 不导出译文/颜色时维持旧语义：仅写一份原文 SRT 到 -o
        from export_srt import write_srt
        original = [
            {"start": int(s["start"]), "end": int(s["end"]), "text": s["text"].strip()}
            for s in segments if isinstance(s, dict) and s.get("text", "").strip()
        ]
        output = Path(args.output) if args.output else input_path.with_suffix(".srt")
        if not write_srt(output, original):
            print("错误: 工程中没有可导出的字幕段", file=sys.stderr)
            return 3
        print(f"已导出 {len(original)} 条字幕: {output}")
        return 0

    written = export_srt_set(project, out_dir, base_name=base_name,
                             colors=want_colors)
    for p in written:
        print(f"已导出: {p}")
    print(f"导出完成: {len(written)} 个文件 → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
