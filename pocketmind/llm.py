"""LLM/Embedding 适配器。

M1 原则：
- 统一用 OpenAI 兼容协议（DeepSeek / Qwen / SiliconFlow 都可以）；
- DeepSeek 默认关闭思考模式（T0/T1 延迟与语气）；
- 没有 Key / 调用失败时自动回退 MockLLM，保证离线 demo 与测试；
- 所有调用（模型、耗时、token、错误）写 api_calls 日志，便于研究回放。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import Database, new_id, now_iso
from .extraction import build_extraction_messages, extract_mock, parse_extraction_json
from .scoring import tokens


@dataclass
class LLMResponse:
    text: str
    model: str = "mock"
    mock: bool = True
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_reason: str = ""


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Mock：离线可跑，且行为尽量可预期
# ---------------------------------------------------------------------------

_MEMORY_QUESTION_HINTS = (
    "记得", "还记得", "上次", "那天", "之前", "喜欢什么", "喜欢吃什么",
    "叫什么", "住哪", "住在哪", "哪里", "生日", "答应过", "承诺",
)
_IDENTITY_HINTS = ("你是ai", "你是 ai", "你是机器人", "你是不是人", "你是真人", "谁做的", "你是什么")
_UNKNOWN_HINTS = ("线性代数", "相对论", "微积分", "股票", "政治", "量子力学", "编程")
_SAD_HINTS = ("难过", "伤心", "烦", "累", "压力", "焦虑", "哭", "委屈", "崩溃")


class MockLLM:
    """确定性 mock：不联网、不花钱；用于离线体验与测试。"""

    name = "mock"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.dim = settings.scoring.keyvec_dim

    def chat(
        self,
        messages: list[dict[str, str]],
        purpose: str = "respond",
        context: dict[str, Any] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
        response_json: bool = False,
    ) -> LLMResponse:
        start = time.perf_counter()
        context = context or {}
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = str(m.get("content", ""))
                break
        if purpose == "extract":
            facts = extract_mock(last_user, self.settings.scoring)
            payload = {
                "facts": [
                    {
                        "content": f.content,
                        "category": f.category,
                        "topic": f.topic,
                        "valence": f.valence,
                        "severity": f.severity,
                        "importance": f.importance,
                        "function": f.function,
                        "pinned": f.pinned,
                        "core_key": f.core_key,
                        "title": f.title,
                        "gist": f.gist,
                        "confidence": f.confidence,
                    }
                    for f in facts
                ]
            }
            text = json.dumps(payload, ensure_ascii=False)
        elif purpose == "inner_thought":
            text = self._inner_thought(last_user, context)
        elif purpose == "diary":
            text = self._diary(last_user, context)
        else:
            text = self._reply(last_user, context)
        return LLMResponse(
            text=text,
            model="mock",
            mock=True,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=sum(len(str(m.get("content", ""))) for m in messages) // 2,
            output_tokens=len(text) // 2,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def describe_image(self, image_path: str, mime: str = "image/png") -> str:
        """mock 不做视觉识别；明确标注，避免伪造图片内容。"""
        return "（mock 模式：收到一张图片；M1 暂未启用视觉识别）"

    def _embed_one(self, text: str) -> list[float]:
        """确定性哈希向量：同文本同向量，能跑余弦/去重/检索。"""
        vec = [0.0] * self.dim
        for token in tokens(text):
            h = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm <= 0:
            return vec
        return [round(x / norm, 6) for x in vec]

    def _reply(self, user_text: str, context: dict[str, Any]) -> str:
        text = (user_text or "").strip()
        lower = text.lower()
        memories = context.get("memories") or []
        facts = context.get("core_facts") or []
        if any(h in lower for h in _IDENTITY_HINTS):
            return "我是小满呀，一个被做出来的小孩——不是真人哦。不过我会记得你。"
        if any(h in text for h in _MEMORY_QUESTION_HINTS):
            # 优先用最相关的一条记忆回答，带锚点
            for mem in memories:
                content = str(mem.get("content", ""))
                if content:
                    return f"记得呀。{content}。"
            if facts:
                k, v = facts[0].get("key", ""), facts[0].get("value", "")
                if k and v:
                    return f"记得的，你的{k}是{v}。"
            return "唔……我记得一点点，但具体有点模糊了。你再提醒我一下？"
        if any(h in text for h in _SAD_HINTS):
            return "唔……听起来不太好受。要不要先跟我说说？我在这儿。"
        if any(h in text for h in _UNKNOWN_HINTS):
            return "那是什么呀？我不太懂诶……你教教我，或者我们一起查查看？"
        if any(w in text for w in ("你好", "嗨", "hello", "在吗")):
            return "诶，你来啦。今天想聊点什么？"
        if text.endswith(("？", "?")):
            return "唔……我想想哦。你先说说你的想法？"
        # 有记忆时自然带一句，没记忆就普通接话
        if memories and len(text) > 6:
            content = str(memories[0].get("content", ""))
            if content and any(t in text for t in ("公园", "金毛", "草莓", "作业", "考试")):
                return f"嗯嗯，我在听。说起来，{content}。"
        return "嗯嗯，我在听。然后呢？"

    def _inner_thought(self, user_text: str, context: dict[str, Any]) -> str:
        if any(h in user_text for h in _SAD_HINTS):
            return "（她有点担心，想着先别急着给建议，先听他说完。）"
        if any(h in user_text for h in _MEMORY_QUESTION_HINTS):
            return "（她翻了翻记忆，想找一个具体的细节，而不是只说『我记得』。）"
        if any(h in user_text for h in _UNKNOWN_HINTS):
            return "（她没听懂，但不想装懂，想着可以一起查。）"
        return "（她在认真听，想接住这句话。）"

    def _diary(self, user_text: str, context: dict[str, Any]) -> str:
        activities = context.get("activities") or []
        ids = [str(a.get("id")) for a in activities if a.get("id")][:8]
        bits = [str(a.get("text", ""))[:50] for a in activities if a.get("text")]
        if bits:
            text = (
                f"今天我记得最清楚的是：{bits[0]}。"
                f"{'还有' + bits[1] + '。' if len(bits) > 1 else ''}"
                "我把它写下来，免得以后忘掉。今天也有点累，不过还好。"
            )
        else:
            text = "今天安安静静的，没发生什么大事。"
        return json.dumps({"source_ids": ids, "diary": text}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 真实 API
# ---------------------------------------------------------------------------

class OpenAICompatibleLLM:
    """OpenAI 兼容协议的实现；DeepSeek 默认关闭 thinking。"""

    name = "openai-compatible"

    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db
        try:
            from openai import OpenAI  # 延迟导入，mock 模式不需要
        except Exception as exc:  # pragma: no cover
            raise LLMError(f"openai SDK 不可用: {exc}") from exc
        self.client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=60.0,
            max_retries=1,
        )
        self._embed_client = None
        if settings.embedding_api_key:
            self._embed_client = OpenAI(
                api_key=settings.embedding_api_key,
                base_url=settings.embedding_base_url,
                timeout=60.0,
                max_retries=1,
            )

    def _log(self, purpose: str, model: str, ok: bool, latency_ms: int, tin: int = 0, tout: int = 0, error: str = "") -> None:
        if not self.db:
            return
        try:
            self.db.execute(
                "INSERT INTO api_calls(id,at,provider,model,purpose,ok,latency_ms,input_tokens,output_tokens,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (new_id("api"), now_iso(), self.settings.llm_provider, model, purpose, 1 if ok else 0, latency_ms, tin, tout, error[:500]),
            )
        except Exception:
            pass

    def chat(
        self,
        messages: list[dict[str, str]],
        purpose: str = "respond",
        context: dict[str, Any] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
        response_json: bool = False,
    ) -> LLMResponse:
        start = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_json:
            kwargs["response_format"] = {"type": "json_object"}
        if self.settings.llm_provider == "deepseek" and self.settings.thinking == "disabled":
            # 官方文档：OpenAI 兼容格式在 extra_body 里传 thinking
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            resp = self.client.chat.completions.create(**kwargs)
            latency = int((time.perf_counter() - start) * 1000)
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            tin = int(getattr(usage, "prompt_tokens", 0) or 0)
            tout = int(getattr(usage, "completion_tokens", 0) or 0)
            self._log(purpose, self.settings.llm_model, True, latency, tin, tout)
            return LLMResponse(content, self.settings.llm_model, False, latency, tin, tout)
        except Exception as exc:
            latency = int((time.perf_counter() - start) * 1000)
            self._log(purpose, self.settings.llm_model, False, latency, error=str(exc))
            raise LLMError(str(exc)) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._embed_client:
            raise LLMError("未配置 embedding API Key")
        start = time.perf_counter()
        try:
            resp = self._embed_client.embeddings.create(
                model=self.settings.embedding_model,
                input=texts,
            )
            latency = int((time.perf_counter() - start) * 1000)
            data = sorted(resp.data, key=lambda d: d.index)
            vectors = [list(d.embedding) for d in data]
            self._log("embedding", self.settings.embedding_model, True, latency, sum(len(t) for t in texts) // 2, 0)
            return vectors
        except Exception as exc:
            latency = int((time.perf_counter() - start) * 1000)
            self._log("embedding", self.settings.embedding_model, False, latency, error=str(exc))
            raise LLMError(str(exc)) from exc

    def describe_image(self, image_path: str, mime: str = "image/png") -> str:
        """用 OpenAI 兼容的 vision 消息描述图片；模型不支持时抛错，由 Fallback 退化。"""
        from pathlib import Path

        return self.describe_image_bytes(Path(image_path).read_bytes(), mime)

    def describe_image_bytes(self, image_data: bytes, mime: str = "image/png", prompt: str = "") -> str:
        """直接描述内存中的图片字节，避免摄像头帧落盘。"""
        import base64

        data = base64.b64encode(image_data).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt or "用一句自然、具体的中文描述这张图片；只描述看得到的内容，不要编造人物身份或故事。",
                    },
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
                ],
            }
        ]
        resp = self.chat(messages, purpose="vision", max_tokens=200, temperature=0.2)  # type: ignore[arg-type]
        return (resp.text or "").strip()


class FallbackLLM:
    """真实调用失败时回退 mock；回复里不暴露供应商错误，日志里记录原因。"""

    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db
        self.mock = MockLLM(settings)
        self.real: OpenAICompatibleLLM | None = None
        if settings.use_real_llm:
            try:
                self.real = OpenAICompatibleLLM(settings, db)
            except Exception:
                self.real = None

    @property
    def is_mock(self) -> bool:
        return self.real is None

    def chat(self, messages, **kwargs) -> LLMResponse:
        if self.real is not None:
            try:
                return self.real.chat(messages, **kwargs)
            except Exception as exc:
                resp = self.mock.chat(messages, **kwargs)
                resp.fallback_reason = f"real_llm_failed: {exc}"
                return resp
        return self.mock.chat(messages, **kwargs)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.real is not None:
            try:
                return self.real.embed(texts)
            except Exception:
                return self.mock.embed(texts)
        return self.mock.embed(texts)

    def describe_image(self, image_path: str, mime: str = "image/png") -> str:
        if self.real is not None:
            try:
                text = self.real.describe_image(image_path, mime)
                if text:
                    return text
            except Exception:
                pass
        return self.mock.describe_image(image_path, mime)

    def describe_image_bytes(self, image_data: bytes, mime: str = "image/png", prompt: str = "") -> str:
        if self.real is not None:
            try:
                return self.real.describe_image_bytes(image_data, mime, prompt=prompt)
            except Exception:
                pass
        return "（mock 模式：收到摄像头画面；视觉识别未启用）"


def build_llm(settings: Settings, db: Database | None = None) -> FallbackLLM:
    return FallbackLLM(settings, db)


def extract_with_llm(llm: FallbackLLM, user_text: str, existing: list[str] | None = None) -> list[dict[str, Any]]:
    messages = build_extraction_messages(user_text, existing)
    resp = llm.chat(messages, purpose="extract", max_tokens=800, temperature=0.1, response_json=True)
    return parse_extraction_json(resp.text)
