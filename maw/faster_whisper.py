# pyright: reportAny=false, reportAttributeAccessIssue=false, reportMissingParameterType=false, reportMissingTypeArgument=false, reportMissingTypeStubs=false, reportReturnType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnusedCallResult=false, reportUnusedVariable=false, reportImplicitStringConcatenation=false, reportArgumentType=false, reportIndexIssue=false

"""本地 faster-whisper ASR 引擎（默认引擎，GPU 加速，无需 API Key）。

- 模型：本地目录（FASTER_WHISPER_MODEL_PATH，如
  /home/ritsuko/Documents/faster-whisper-large-v3/）或尺寸名（large-v3/small...）
- faster-whisper 的 segment.words（字/词级时间戳）→ MAW items（整数毫秒）
- 说话人分离：faster-whisper 无内置 diarization（需 pyannote 等外部模型），
  enable_speaker 时给出警告并忽略，不伪造 speaker 标签
- VAD：默认开（silero），长音频自动分块

返回 {"text", "language", "items"}，与 soniox/qwen 引擎同构，可直接交给
build_segments() 切句（决策：本地引擎默认）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from generate_subtitle_qwen_api import (
    WESTERN_MAX_WORDS,
    WESTERN_MIN_WORDS,
    is_cjk_char,
    split_segments_auto,
)

# 默认模型：本机已有 large-v3（faster-whisper 格式，~2.9GB）
DEFAULT_MODEL = "large-v3"
DEFAULT_MODEL_PATH = "/home/ritsuko/Documents/faster-whisper-large-v3"

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 模型加载是耗时操作（large-v3 约 1-3s GPU 加载），进程内缓存避免重复加载
_model_cache: dict = {}


# ===== 配置（.env，与 Soniox 版同样的零依赖解析） =====

def _load_env_file() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    config: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        config[k.strip()] = v.strip()
    return config


def load_config() -> dict:
    """合并 .env 与系统环境变量（系统环境变量优先）。"""
    env = _load_env_file()

    def pick(key: str, default: str = "") -> str:
        return os.getenv(key) or env.get(key, default)

    return {
        "model_path": pick("FASTER_WHISPER_MODEL_PATH", DEFAULT_MODEL_PATH)
                      or DEFAULT_MODEL_PATH,
        "model": pick("FASTER_WHISPER_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        "device": pick("FASTER_WHISPER_DEVICE", "auto") or "auto",
        "compute_type": pick("FASTER_WHISPER_COMPUTE_TYPE", "") or "",
        "vad": (pick("FASTER_WHISPER_VAD", "true") or "true").lower() != "false",
    }


# ===== faster-whisper 封装 =====

def _resolve_model_arg(config: dict, model_size: str) -> str:
    """模型解析：显式目录 > 配置路径（尺寸匹配时）> 尺寸名（自动下载）。"""
    model_spec = model_size or config["model"]
    explicit = Path(model_spec).expanduser()
    if (explicit / "model.bin").exists() or (explicit / "model.ct2").exists():
        return str(explicit)
    configured = Path(config["model_path"]).expanduser()
    if (model_spec == config["model"] or not model_size) and configured.is_dir():
        return str(configured)
    return model_spec


def _resolve_device(config: dict) -> tuple[str, str]:
    """device auto → cuda/cpu；compute 为空时按设备给默认精度。"""
    device = config["device"]
    if device == "auto":
        try:
            from ctranslate2 import get_cuda_device_count
            device = "cuda" if get_cuda_device_count() > 0 else "cpu"
        except ImportError:
            device = "cpu"
    compute = config["compute_type"]
    if not compute:
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def _load_model(config: dict, model_size: str, on_status=print):
    """加载 WhisperModel；model_size 优先于配置（CLI --model 覆盖）。"""
    device, compute = _resolve_device(config)
    key = (model_size, device, compute)
    if key in _model_cache:
        return _model_cache[key]
    from faster_whisper import WhisperModel

    model_arg = _resolve_model_arg(config, model_size)
    on_status(f"[faster-whisper] 正在加载模型: {model_arg} "
              f"(device={device}, compute={compute})...")
    t0 = time.perf_counter()
    model = WhisperModel(model_arg, device=device, compute_type=compute)
    on_status(f"[faster-whisper] 模型加载完成（{time.perf_counter() - t0:.1f}s）")
    _model_cache[key] = model
    return model


def segments_to_items(segments) -> list[dict]:
    """faster-whisper segments → MAW items（整数毫秒）。

    word_timestamps=True 时 words 有精确边界；个别 word 缺时间戳时
    用 segment 边界兜底；零宽 item 直接跳过。

    空格语言（英/德/西…）的词间空格：faster-whisper 的 word 不带前导空格，
    这里补上（与 soniox merge_word_fragments 的 token 语义一致），保证
    MAW 的逐词拼接后文本可读；CJK 逐字不受影响。
    """
    items: list[dict] = []
    prev = ""
    for seg in segments:
        words = list(getattr(seg, "words", None) or [])
        if words:
            for w in words:
                start = int(round(w.start * 1000))
                end = int(round(w.end * 1000))
                if end <= start:
                    continue
                word = w.word or ""
                if (word and not word[0].isspace() and prev and not prev[-1].isspace()
                        and not is_cjk_char(word[0])):
                    word = " " + word
                items.append({"text": word, "start": start, "end": end})
                prev = word
        else:
            start = int(round(seg.start * 1000))
            end = int(round(seg.end * 1000))
            items.append({"text": seg.text.strip(), "start": start, "end": end})
            prev = seg.text or ""
    return items


def transcribe(audio_path: str, config: dict, *,
               language: str | None = None,
               model_size: str | None = None,
               enable_speaker: bool = False,
               on_status=print) -> dict:
    """本地转写：加载模型 → transcribe（word_timestamps）→ items。

    返回 {"text", "language", "items"}（整数毫秒），供 build_segments() 切句。
    """
    if enable_speaker:
        on_status("[faster-whisper] [警告] 本地引擎无说话人分离（faster-whisper 不含 "
                  "diarization），已忽略 --speaker，speaker 标签不写入")

    model = _load_model(config, model_size or "", on_status=on_status)
    on_status(f"[faster-whisper] 开始转写: {Path(audio_path).name}")
    t0 = time.perf_counter()
    segments, info = model.transcribe(
        audio_path,
        language=language or None,
        word_timestamps=True,
        vad_filter=config.get("vad", True),
    )
    items = segments_to_items(segments)
    elapsed = time.perf_counter() - t0
    duration = getattr(info, "duration", 0) or 0
    lang = getattr(info, "language", "") or ""
    if duration > 0:
        on_status(f"[faster-whisper] 转写完成（{elapsed:.1f}s，"
                  f"{elapsed / duration:.2f}x 实时）| language={lang} "
                  f"items={len(items)}")
    else:
        on_status(f"[faster-whisper] 转写完成（{elapsed:.1f}s）| language={lang} "
                  f"items={len(items)}")
    text = "".join(it["text"] for it in items)
    return {"text": text, "language": lang, "items": items}


def build_segments(items: list[dict], *, max_len: int, min_len: int,
                   gap_split_ms: int,
                   max_words: int = WESTERN_MAX_WORDS,
                   min_words: int = WESTERN_MIN_WORDS) -> list[dict]:
    """与 soniox 同构：按静音组自动选择 CJK/英文切句（本地引擎无 speaker，直接全量切）。"""
    return split_segments_auto(
        items, max_len=max_len, min_len=min_len, gap_split_ms=gap_split_ms,
        max_words=max_words, min_words=min_words,
    )
