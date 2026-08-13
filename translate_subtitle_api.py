# pyright: reportAny=false, reportAttributeAccessIssue=false, reportMissingParameterType=false, reportMissingTypeArgument=false, reportMissingTypeStubs=false, reportReturnType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnusedCallResult=false, reportUnusedVariable=false, reportImplicitStringConcatenation=false, reportArgumentType=false, reportIndexIssue=false

"""字幕翻译 CLI（决策 26：OpenAI 兼容 API 为主通道；决策 42：译文写回 mosp）。

输入 subs.srt 或 .mosp 工程文件 → 按批次送 OpenAI 兼容接口（base_url + api_key +
model，支持 429/5xx 指数退避重试）。

- 默认模式：输出同时间轴的目标语言 SRT（历史行为，决策 26）。
- `--write-mosp`：译文写回 .mosp 的 `segment.translation`，并声明顶层
  `translation_language`（决策 42；导出 SRT 用 mosp_to_srt.py / serve.py 导出端点）。
- `--only-empty`（仅 .mosp + --write-mosp 时有意义）：只翻译尚无译文的段。

配置优先级：CLI 参数 > 环境变量（TRANSLATE_API_KEY / TRANSLATE_BASE_URL /
TRANSLATE_MODEL）> .env（TRANSLATE_* 同键）。

用法示例：
    python translate_subtitle_api.py subs.srt --target zh \
        --api-key sk-xxx --base-url https://api.deepseek.com/v1 --model deepseek-chat
    python translate_subtitle_api.py subs.mosp -o subs_cn.srt --target zh
    python translate_subtitle_api.py subs/subs.mosp --write-mosp --only-empty --target zh
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

import requests

from generate_subtitle_qwen_api import configure_console_output

DEFAULT_SYSTEM_PROMPT = (
    "You are a subtitle translator. Translate the given subtitles accurately and "
    "naturally, keeping proper nouns and technical terms intact. Output only the "
    "translated text, one line per subtitle entry, prefixed with its number."
)

_ENV_FILE = Path(__file__).resolve().parent / ".env"

# —— 配置 ——

def resolve_config(*, base_url: str = "", model: str = "", api_key: str = "",
                   target: str = "zh", batch_size: int = 20, max_retries: int = 3,
                   system_prompt: str = "") -> dict:
    """按 CLI > 环境变量 > .env 的优先级解析翻译配置（serve.py 也复用此入口）。"""
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    def pick(cli: str, env_key: str, default: str = "") -> str:
        return cli or os.getenv(env_key) or env.get(env_key, default)

    config = {
        "base_url": pick(base_url, "TRANSLATE_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": pick(model, "TRANSLATE_MODEL", "gpt-4o-mini"),
        "api_key": pick(api_key, "TRANSLATE_API_KEY"),
        "target": target or "zh",
        "batch_size": batch_size,
        "max_retries": max_retries,
        "system_prompt": system_prompt or DEFAULT_SYSTEM_PROMPT,
    }
    if not config["api_key"]:
        raise RuntimeError(
            "未配置翻译 API Key：请用 --api-key，或设置环境变量 / .env 的 "
            "TRANSLATE_API_KEY（可申请 OpenAI 或任意兼容服务密钥）")
    return config


def load_config(args) -> dict:
    try:
        return resolve_config(base_url=args.base_url, model=args.model,
                              api_key=args.api_key, target=args.target,
                              batch_size=args.batch_size,
                              max_retries=args.max_retries,
                              system_prompt=args.system_prompt)
    except RuntimeError as e:
        print(f"[错误] {e}", file=sys.stderr)
        raise SystemExit(1) from e


# —— 输入解析 ——

def parse_subtitles(path: Path) -> list[dict]:
    """srt 或 .mosp → [{start, end, text}]（时间毫秒；srt 解析为毫秒）。"""
    if path.suffix == ".mosp":
        data = json.loads(path.read_text(encoding="utf-8"))
        segments = data.get("segments", [])
        return [{"start": int(s["start"]), "end": int(s["end"]),
                 "text": s["text"].strip()} for s in segments if s.get("text", "").strip()]
    return parse_srt(path)


def parse_mosp(path: Path) -> tuple[dict, list[dict]]:
    """.mosp → (完整工程 dict, [{start, end, text, translation, index}]）。

    带 index 与已有 translation，供 --write-mosp / --only-empty 回写。
    """
    project = json.loads(path.read_text(encoding="utf-8"))
    entries: list[dict] = []
    for i, s in enumerate(project.get("segments", [])):
        if not s.get("text", "").strip():
            continue
        entries.append({"start": int(s["start"]), "end": int(s["end"]),
                        "text": s["text"].strip(),
                        "translation": s.get("translation", ""),
                        "index": i})
    return project, entries


def parse_srt(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[dict] = []
    cur: dict | None = None
    for line in lines:
        line = line.strip()
        if not line:
            cur = None
            continue
        if cur is None:
            if "-->" in line:
                cur = {"timecode": line, "texts": []}
                blocks.append(cur)
        else:
            cur["texts"].append(line)
    out = []
    for b in blocks:
        tc = b["timecode"].split("-->")
        if len(tc) != 2:
            continue
        text = "\n".join(b["texts"]).strip()
        if not text:
            continue
        out.append({"start": _tc_to_ms(tc[0].strip()),
                    "end": _tc_to_ms(tc[1].strip()),
                    "text": text})
    return out


def _tc_to_ms(tc: str) -> int:
    parts = tc.replace(",", ".").split(":")
    h, m, s = (float(x) for x in parts)
    return int((h * 3600 + m * 60 + s) * 1000)


# —— 输出 ——

def ms_to_tc(ms: int) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


def render_srt(entries: list[dict]) -> str:
    parts = []
    for i, e in enumerate(entries, 1):
        parts.append(f"{i}\n{ms_to_tc(e['start'])} --> {ms_to_tc(e['end'])}\n{e['text']}")
    return "\n\n".join(parts) + "\n"


# —— 翻译调用（OpenAI 兼容，429/5xx 指数退避） ——

def _call_chat(config: dict, user_text: str) -> str:
    url = f"{config['base_url']}/chat/completions"
    headers = {"Authorization": f"Bearer {config['api_key']}",
               "Content-Type": "application/json"}
    payload = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": config["system_prompt"]},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.3,
    }
    delay = 2.0
    for attempt in range(config["max_retries"] + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=120)
        except requests.RequestException as e:
            if attempt >= config["max_retries"]:
                raise RuntimeError(f"请求失败（已重试 {attempt} 次）: {e}")
            print(f"[translate] [警告] 网络错误（第 {attempt + 1} 次）: {e}，"
                  f"{int(delay)}s 后重试")
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 200:
            try:
                return r.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as e:
                raise RuntimeError(f"响应格式异常: {e}") from e
        if r.status_code in (429, 500, 502, 503, 529):
            if attempt >= config["max_retries"]:
                raise RuntimeError(f"服务端错误（HTTP {r.status_code}，已重试 "
                                   f"{attempt} 次）: {r.text[:200]}")
            print(f"[translate] [警告] HTTP {r.status_code}（第 {attempt + 1} 次），"
                  f"{int(delay)}s 后重试")
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(f"API 错误（HTTP {r.status_code}）: {r.text[:300]}")
    raise RuntimeError("翻译请求未完成")  # 不可达，防御


_NUM_RE = re.compile(r"^\s*(\d{1,4})[.、:：)]\s*(.+)$")


def translate_entries(config: dict, entries: list[dict],
                      on_batch: Callable[[list[dict], int, int], None] | None = None) -> list[dict]:
    """按批次翻译，返回同序 [{start, end, text(译), index?}]。

    on_batch(batch_out, done, total)：每批翻译完成后回调（serve.py 增量写回 mosp
    与编辑器进度显示用；batch_out = 本批 [{start, end, text(译), index?}]）。
    """
    out: list[dict] = []
    batch_size = config["batch_size"]
    for i in range(0, len(entries), batch_size):
        batch = entries[i:i + batch_size]
        numbered = "\n".join(f"{j + 1}. {e['text']}" for j, e in enumerate(batch))
        print(f"[translate] 正在翻译第 {i + 1}–{min(i + batch_size, len(entries))}/"
              f"{len(entries)} 条（模型: {config['model']}）...")
        content = _call_chat(config, numbered)

        parsed: dict[int, str] = {}
        for line in content.splitlines():
            m = _NUM_RE.match(line.strip())
            if m:
                parsed[int(m.group(1))] = m.group(2).strip()
        batch_out: list[dict] = []
        for j, e in enumerate(batch):
            translated = parsed.get(j + 1, "").strip()
            batch_out.append({"start": e["start"], "end": e["end"],
                              "text": translated or e["text"],  # 缺译兜底原文
                              **({"index": e["index"]} if "index" in e else {})})
        out.extend(batch_out)
        if on_batch:
            on_batch(batch_out, min(i + batch_size, len(entries)), len(entries))
        if not parsed:
            print("[translate] [警告] 响应未按编号返回，已用原文兜底（可换更稳定的模型）")
    return out


# —— 写回 mosp（决策 42） ——

def write_mosp(project: dict, translated: list[dict], target: str,
               output_path: Path) -> None:
    """把译文写回 mosp：segment.translation + 顶层 translation_language。"""
    for e in translated:
        if "index" not in e:
            continue
        project["segments"][e["index"]]["translation"] = e["text"]
    project["translation_language"] = target
    output_path.write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="字幕翻译（OpenAI 兼容 API）：srt/.mosp → 目标语言 SRT；"
                    "--write-mosp 把译文写回 .mosp（决策 42）")
    parser.add_argument("input", help="输入 subs.srt 或 subs.mosp")
    parser.add_argument("-o", "--output", help="输出路径（默认 <输入>_cn.srt；"
                                               "--write-mosp 时默认写回输入文件）")
    parser.add_argument("--write-mosp", action="store_true",
                        help="译文写回 .mosp 的 segment.translation + "
                             "translation_language（导出 SRT 请用 mosp_to_srt.py）")
    parser.add_argument("--only-empty", action="store_true",
                        help="只翻译尚无译文的段（仅 .mosp + --write-mosp 时有意义）")
    parser.add_argument("--target", default="zh", help="目标语言（如 zh/en/ja，默认 zh）")
    parser.add_argument("--api-key", default=None, help="API Key（默认读 TRANSLATE_API_KEY / .env）")
    parser.add_argument("--base-url", default=None, help="接口地址（默认 https://api.openai.com/v1）")
    parser.add_argument("--model", default=None, help="模型名（默认 gpt-4o-mini）")
    parser.add_argument("--batch-size", type=int, default=20, help="每批条数（默认 20）")
    parser.add_argument("--max-retries", type=int, default=3, help="429/5xx 重试次数（默认 3）")
    parser.add_argument("--system-prompt", default=None, help="覆盖系统提示词")
    args = parser.parse_args()
    configure_console_output()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在 - {input_path}", file=sys.stderr)
        raise SystemExit(1)
    config = load_config(args)

    # 把目标语言写进提示词（comfy 同款做法：语言名直接进 prompt）
    config["system_prompt"] = config["system_prompt"].replace(
        "given subtitles", f"given subtitles into {config['target']}")

    if args.write_mosp:
        if input_path.suffix != ".mosp":
            print("错误: --write-mosp 只支持 .mosp 输入", file=sys.stderr)
            raise SystemExit(2)
        project, entries = parse_mosp(input_path)
        if not entries:
            print("错误: 没有可翻译的字幕条目", file=sys.stderr)
            raise SystemExit(2)
        if args.only_empty:
            entries = [e for e in entries if not e.get("translation", "").strip()]
            if not entries:
                print("没有空缺译文：所有条目均已有译文，无需翻译")
                return
        print(f"[准备] 已载入 {len(entries)} 条待译字幕（目标语言: {config['target']}，"
              f"只翻空缺: {args.only_empty}）")

        t0 = time.perf_counter()
        translated = translate_entries(config, entries)
        elapsed = time.perf_counter() - t0
        write_mosp(project, translated, config["target"],
                   Path(args.output) if args.output else input_path)
        print(f"翻译完成: 译文已写回 mosp（{len(translated)} 条，用时 {elapsed:.1f}s，"
              f"目标语言 {config['target']}）")
        return

    entries = parse_subtitles(input_path)
    if not entries:
        print("错误: 没有可翻译的字幕条目", file=sys.stderr)
        raise SystemExit(2)
    print(f"[准备] 已载入 {len(entries)} 条字幕（目标语言: {config['target']}）")

    t0 = time.perf_counter()
    translated = translate_entries(config, entries)
    elapsed = time.perf_counter() - t0

    output = Path(args.output) if args.output else input_path.with_name(
        f"{input_path.stem}_cn.srt")
    output.write_text(render_srt(translated), encoding="utf-8")
    print(f"翻译完成: {output}（{len(translated)} 条，用时 {elapsed:.1f}s，"
          f"目标语言 {config['target']}）")


if __name__ == "__main__":
    main()
