# pyright: reportAny=false, reportMissingParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false

"""把 MAWE 编辑后的 .mosp 工程文件导出为 SRT（供 p3 字幕/弹幕烧录）。

用法：python mosp_to_srt.py <input.mosp> [-o output.srt]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from generate_subtitle_qwen_api import generate_srt


def main() -> int:
    parser = argparse.ArgumentParser(description=".mosp 工程 → SRT")
    parser.add_argument("input", help="输入 .mosp 工程文件")
    parser.add_argument("-o", "--output", help="输出 SRT（默认与输入同名 .srt）")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在 - {input_path}", file=sys.stderr)
        return 1
    data = json.loads(input_path.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    if not segments:
        print("错误: 工程中没有字幕段", file=sys.stderr)
        return 2
    output = Path(args.output) if args.output else input_path.with_suffix(".srt")
    output.write_text(generate_srt(segments), encoding="utf-8")
    print(f"已导出 {len(segments)} 条字幕: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
