"""TTS 接口预留（本地 TTS 方案见 docs/TTS-本地部署方案.md）。

当前只定义协议与空实现，不下载模型、不启动服务；
后续把 LocalTTSProvider 接到 GPT-SoVITS / CosyVoice / Piper 等的本地 HTTP 服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class TTSResult:
    ok: bool
    audio_path: str = ""
    mime: str = "audio/wav"
    duration_ms: int = 0
    error: str = ""


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, out_path: Path, voice: str = "", speed: float = 1.0) -> TTSResult:
        ...


class NullTTS:
    """占位实现：不产生音频，只返回未启用。"""

    name = "null"

    def synthesize(self, text: str, out_path: Path, voice: str = "", speed: float = 1.0) -> TTSResult:
        return TTSResult(ok=False, error="TTS 未启用（预留接口）")


class LocalHTTPTTS:
    """示例：把 text 发给本地 TTS 服务的 /tts，返回音频字节。

    真实实现需要按所选引擎的 API 调整（GPT-SoVITS/CosyVoice/Piper 不同）。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:9880", name: str = "local-http"):
        self.base_url = base_url.rstrip("/")
        self.name = name

    def synthesize(self, text: str, out_path: Path, voice: str = "", speed: float = 1.0) -> TTSResult:
        import httpx

        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    f"{self.base_url}/tts",
                    json={"text": text, "voice": voice, "speed": speed, "format": "wav"},
                )
                resp.raise_for_status()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(resp.content)
            return TTSResult(ok=True, audio_path=str(out_path))
        except Exception as exc:
            return TTSResult(ok=False, error=str(exc))


def build_tts() -> TTSProvider:
    return NullTTS()
