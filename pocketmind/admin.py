"""本地管理台后端：API/模型、人格与 Prompt、参数、数据浏览。

只监听 127.0.0.1（由启动器/uvicorn 决定），定位是个人科研的本地工具；
API Key 不再明文写入 settings.json：AES-256-GCM + Windows DPAPI 加密存储。
"""

from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import fields
from datetime import date
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .admin_schema import (
    EMBEDDING_PRESETS,
    PROVIDER_PRESETS,
    SCORING_GROUPS,
    THINKING_OPTIONS,
)
from .config import PACKAGE_ROOT, ScoringConfig, Settings
from .db import Database
from .pipeline import ChatEngine
from .prompt_store import (
    PLACEHOLDERS,
    default_template,
    get_template,
    reset_template,
    save_template,
    template_meta,
    template_path,
)
from .prompts import build_system_prompt
from .release_security import release_payload
from .settings_store import load_raw, mask_key, merge_section, save_raw

router = APIRouter(prefix="/api/admin", tags=["admin"])
ADMIN_TEMPLATE = PACKAGE_ROOT / "templates" / "admin.html"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _engine(request: Request) -> ChatEngine:
    return request.app.state.engine


def _settings(request: Request) -> Settings:
    return request.app.state.engine.settings


def _reload_engine(request: Request) -> ChatEngine:
    old = _engine(request)
    new_settings = Settings(data_dir=old.settings.data_dir)
    new_engine = ChatEngine(new_settings)
    request.app.state.engine = new_engine
    request.app.state.settings = new_settings
    return new_engine


def _persona_payload(engine: ChatEngine) -> dict[str, Any]:
    p = engine.persona_card
    data = p.to_dict()
    data["entity_id"] = p.entity_id
    data["version"] = p.version
    data["effective_age"] = p.effective_age()
    return data


def _normalize_birthday(value: Any) -> str:
    """接受 MM-DD 或 YYYY-MM-DD，统一存 MM-DD；非法抛 ValueError。"""
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split("-")
    if len(parts) == 3:
        parts = parts[1:]
    if len(parts) != 2:
        raise ValueError("生日格式应为 MM-DD，例如 10-26")
    try:
        month, day = int(parts[0]), int(parts[1])
        date(2000, month, day)  # 允许 2-29，用闰年校验
    except ValueError as exc:
        raise ValueError("生日不是有效日期（MM-DD）") from exc
    return f"{month:02d}-{day:02d}"


def _remove_section_key(path, section: str) -> None:
    """从 settings.json 删除旧的明文 api_key，不碰同段其它设置。"""
    data = load_raw(path)
    current = data.get(section)
    if isinstance(current, dict) and "api_key" in current:
        current.pop("api_key", None)
        data[section] = current
        save_raw(path, data)


def _release_security_payload(settings: Settings) -> dict[str, Any]:
    return release_payload(settings)


def _settings_payload(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    s = engine.settings
    persona = engine.persona_card
    relationship = engine.persona.relationship(persona.entity_id)
    core = engine.memory.core_facts(persona.entity_id)
    preview = build_system_prompt(persona, relationship, core, [], s.scoring)
    raw = load_raw(s.settings_file)
    file_llm = raw.get("llm") or {}
    file_emb = raw.get("embedding") or {}
    secret = s.secret_status()
    llm_source = "encrypted" if secret.get("llm_key_present") else (
        "cleared" if "api_key" in file_llm and not file_llm.get("api_key")
        else ("env" if s.llm_api_key else "")
    )
    emb_source = "encrypted" if secret.get("embedding_key_present") else (
        "cleared" if "api_key" in file_emb and not file_emb.get("api_key")
        else ("env" if s.embedding_api_key else "")
    )
    return {
        "llm": {
            "provider": s.llm_provider,
            "base_url": s.llm_base_url,
            "model": s.llm_model,
            "thinking": s.thinking,
            "temperature": s.llm_temperature,
            "max_tokens": s.llm_max_tokens,
            "api_key_set": bool(s.llm_api_key),
            "api_key_masked": mask_key(s.llm_api_key),
            "api_key_source": llm_source,
            "api_key_encrypted": bool(secret.get("llm_key_present")),
        },
        "embedding": {
            "provider": s.embedding_provider,
            "base_url": s.embedding_base_url,
            "model": s.embedding_model,
            "dim": s.embedding_dim_hint,
            "api_key_set": bool(s.embedding_api_key),
            "api_key_masked": mask_key(s.embedding_api_key),
            "api_key_source": emb_source,
            "api_key_encrypted": bool(secret.get("embedding_key_present")),
        },
        "persona": _persona_payload(engine),
        "prompt": {
            "custom_instructions": persona.custom_instructions,
            "preview": preview,
            "file": "pocketmind/prompts.py",
            "explanation": "身份/人格/关系/记忆门/情绪/核心事实/记忆片段在这里拼装；UI 的『补充指令』会追加在最后，优先级低于身份与边界。",
        },
        "scoring": {f.name: getattr(s.scoring, f.name) for f in fields(ScoringConfig)},
        "scoring_groups": SCORING_GROUPS,
        "providers": PROVIDER_PRESETS,
        "embedding_presets": EMBEDDING_PRESETS,
        "thinking_options": THINKING_OPTIONS,
        "paths": {
            "root": str(s.root),
            "data_dir": str(s.data_dir),
            "db": str(s.db_path),
            "diary": str(s.diary_dir),
            "media": str(s.data_dir / "media"),
            "settings": str(s.settings_file),
            "secrets": str(s.data_dir / "secrets"),
            "env_example": str(s.root / ".env.example"),
        },
        "security": {
            "code_signing": {
                "status": "unsigned",
                "detail": "当前没有代码签名证书；构建为未签名版本。",
            },
            "key_storage": secret,
            "release": _release_security_payload(s),
        },
        "mock": s.mock,
        "data_sources": _data_sources(),
    }


def _data_sources() -> list[dict[str, str]]:
    return [
        {"kind": "memories", "label": "记忆", "desc": "长期记忆（含核心事实/状态/半衰期）"},
        {"kind": "events", "label": "事件", "desc": "每条用户消息形成的事件与打分"},
        {"kind": "messages", "label": "消息", "desc": "对话原文"},
        {"kind": "retrieval_logs", "label": "检索日志", "desc": "候选/选中/分数/gate"},
        {"kind": "relationship_events", "label": "关系变化", "desc": "好感/信任/心境变化"},
        {"kind": "impressions", "label": "印象/疤痕", "desc": "标签+强度+来源"},
        {"kind": "diary_entries", "label": "工作日志", "desc": "每轮系统自动记录（原“日记”，不参与检索）"},
        {"kind": "persona_diaries", "label": "她的日记", "desc": "人格化日记 + 真实活动来源 ID"},
        {"kind": "inner_thoughts", "label": "内心话", "desc": "仅开发者查看，不进检索"},
        {"kind": "annotations", "label": "人工标注", "desc": "评分+备注"},
        {"kind": "media", "label": "媒体", "desc": "图片路径/caption/关联记忆"},
        {"kind": "core_facts", "label": "核心事实", "desc": "pin 住的事实（含有效期/置信度/历史状态）"},
        {"kind": "core_fact_conflicts", "label": "核心事实冲突", "desc": "改口时的旧值/新值与处理结果"},
        {"kind": "skills", "label": "技能池", "desc": "程序性记忆：可复用流程/约定，慢衰、调用加固"},
        {"kind": "night_runs", "label": "夜间流水线", "desc": "整理→技能维护→回验→梦/直接醒的可回放记录"},
        {"kind": "dreams", "label": "梦", "desc": "只归档的梦境条目"},
    ]


# ---------------------------------------------------------------------------
# 设置读写
# ---------------------------------------------------------------------------

def _clean_scoring(values: dict[str, Any]) -> dict[str, Any]:
    valid = {f.name: f for f in fields(ScoringConfig)}
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key not in valid or value is None:
            continue
        field_type = valid[key].type
        try:
            if key == "severity_weights":
                out[key] = {int(k): float(v) for k, v in dict(value).items()}
            elif key == "gate_explicit_cues":
                if isinstance(value, str):
                    out[key] = [
                        x.strip()
                        for x in value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",").split(",")
                        if x.strip()
                    ]
                else:
                    out[key] = [str(x).strip() for x in value if str(x).strip()]
            elif "float" in str(field_type):
                out[key] = float(value)
            elif "int" in str(field_type):
                out[key] = int(value)
            else:
                out[key] = value
        except (TypeError, ValueError):
            continue
    return out


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return _settings_payload(request)


@router.put("/settings")
def put_settings(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    old = _settings(request)
    llm = body.get("llm") or {}
    emb = body.get("embedding") or {}
    scoring = body.get("scoring") or {}

    llm_out: dict[str, Any] = {}
    for key in ("provider", "base_url", "model", "thinking"):
        if llm.get(key) not in (None, ""):
            llm_out[key] = str(llm[key])
    for key, cast in (("temperature", float), ("max_tokens", int)):
        if llm.get(key) not in (None, ""):
            try:
                llm_out[key] = cast(llm[key])
            except (TypeError, ValueError):
                pass
    if llm.get("clear_api_key"):
        if old.secret_store is None:
            raise HTTPException(status_code=500, detail="加密存储不可用，无法安全清除 API Key")
        old.secret_store.delete("llm")
        llm_out["api_key"] = ""  # 显式空值：压制环境变量
    elif llm.get("api_key") and "•" not in str(llm["api_key"]):
        if old.secret_store is None:
            raise HTTPException(status_code=500, detail="加密存储不可用，拒绝保存明文 API Key")
        old.secret_store.put("llm", str(llm["api_key"]).strip())

    emb_out: dict[str, Any] = {}
    for key in ("provider", "base_url", "model"):
        if emb.get(key) not in (None, ""):
            emb_out[key] = str(emb[key])
    if emb.get("dim") not in (None, ""):
        try:
            emb_out["dim"] = int(emb["dim"])
        except (TypeError, ValueError):
            pass
    if emb.get("clear_api_key"):
        if old.secret_store is None:
            raise HTTPException(status_code=500, detail="加密存储不可用，无法安全清除 Embedding Key")
        old.secret_store.delete("embedding")
        emb_out["api_key"] = ""
    elif emb.get("api_key") and "•" not in str(emb["api_key"]):
        if old.secret_store is None:
            raise HTTPException(status_code=500, detail="加密存储不可用，拒绝保存明文 Embedding Key")
        old.secret_store.put("embedding", str(emb["api_key"]).strip())

    if llm_out:
        merge_section(old.settings_file, "llm", llm_out)
    if llm.get("api_key") and not llm.get("clear_api_key"):
        _remove_section_key(old.settings_file, "llm")
    if emb_out:
        merge_section(old.settings_file, "embedding", emb_out)
    if emb.get("api_key") and not emb.get("clear_api_key"):
        _remove_section_key(old.settings_file, "embedding")
    if scoring:
        clean = _clean_scoring(scoring)
        if clean:
            merge_section(old.settings_file, "scoring", clean)
    try:
        _reload_engine(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"保存后重载失败：{exc}") from exc
    return {"ok": True, "settings": _settings_payload(request)}


@router.post("/test_llm")
def test_llm(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    s = _settings(request)
    key = str(body.get("api_key") or "").strip() or s.llm_api_key
    if not key:
        return {"ok": False, "error": "没有 API Key；请先填写或先使用 Mock 模式。"}
    try:
        from openai import OpenAI
    except Exception as exc:
        return {"ok": False, "error": f"openai SDK 不可用：{exc}"}
    base_url = str(body.get("base_url") or s.llm_base_url).rstrip("/")
    model = str(body.get("model") or s.llm_model)
    provider = str(body.get("provider") or s.llm_provider)
    thinking = str(body.get("thinking") or s.thinking)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字母：ok"}],
        "max_tokens": 16,
        "temperature": 0.0,
    }
    if provider == "deepseek" and thinking == "disabled":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    started = time.perf_counter()
    try:
        client = OpenAI(api_key=key, base_url=base_url, timeout=30.0, max_retries=0)
        resp = client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        return {
            "ok": True,
            "model": model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "preview": text[:200],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "latency_ms": int((time.perf_counter() - started) * 1000)}


@router.post("/test_embedding")
def test_embedding(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    s = _settings(request)
    key = str(body.get("api_key") or "").strip() or s.embedding_api_key
    if not key:
        return {"ok": False, "error": "没有 Embedding API Key；可先留空用关键词兜底。"}
    try:
        from openai import OpenAI
    except Exception as exc:
        return {"ok": False, "error": f"openai SDK 不可用：{exc}"}
    base_url = str(body.get("base_url") or s.embedding_base_url).rstrip("/")
    model = str(body.get("model") or s.embedding_model)
    started = time.perf_counter()
    try:
        client = OpenAI(api_key=key, base_url=base_url, timeout=30.0, max_retries=0)
        resp = client.embeddings.create(model=model, input=["PocketMind embedding test"])
        dim = len(resp.data[0].embedding)
        return {"ok": True, "model": model, "dim": dim, "latency_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500], "latency_ms": int((time.perf_counter() - started) * 1000)}


@router.post("/models")
async def list_models(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    s = _settings(request)
    kind = str(body.get("kind") or "llm")
    provider = str(body.get("provider") or (s.llm_provider if kind == "llm" else s.embedding_provider))
    presets = PROVIDER_PRESETS if kind == "llm" else EMBEDDING_PRESETS
    base_url = str(body.get("base_url") or (s.llm_base_url if kind == "llm" else s.embedding_base_url)).rstrip("/")
    key = str(body.get("api_key") or "").strip() or (s.llm_api_key if kind == "llm" else s.embedding_api_key)
    preset = presets.get(provider, {})
    fallback = list(preset.get("models") or [])
    if not key:
        return {"ok": True, "models": fallback, "source": "preset", "note": "未填 Key，显示预置候选。"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
        if resp.status_code >= 400:
            return {"ok": True, "models": fallback, "source": "preset", "note": f"服务商 /models 返回 HTTP {resp.status_code}，用预置候选。"}
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else None
        models = []
        if isinstance(items, list):
            for item in items:
                mid = item.get("id") if isinstance(item, dict) else None
                if mid:
                    models.append(str(mid))
        models = sorted(set(models))
        return {"ok": True, "models": models or fallback, "source": "live" if models else "preset"}
    except Exception as exc:
        return {"ok": True, "models": fallback, "source": "preset", "note": f"在线拉取失败：{str(exc)[:160]}；用预置候选。"}


# ---------------------------------------------------------------------------
# 人格 / Prompt
# ---------------------------------------------------------------------------

@router.get("/persona")
def get_persona(request: Request) -> dict[str, Any]:
    return {"persona": _persona_payload(_engine(request))}


@router.put("/persona")
def put_persona(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    engine = _engine(request)
    allowed = {
        "name", "age", "gender", "species", "origin", "values", "traits",
        "speech_style", "boundaries", "knowledge_boundary", "origin_story",
        "custom_instructions", "age_tracking", "birthday",
    }
    payload = {k: v for k, v in body.items() if k in allowed and v is not None}
    if not payload:
        raise HTTPException(status_code=400, detail="没有可保存的人格字段")
    for key in ("values", "traits", "boundaries"):
        if key in payload and isinstance(payload[key], str):
            payload[key] = [
                x.strip()
                for x in payload[key].replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",").replace("\n", ",").split(",")
                if x.strip()
            ]
    if "speech_style" in payload and isinstance(payload["speech_style"], str):
        try:
            payload["speech_style"] = json.loads(payload["speech_style"])
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="speech_style 不是合法 JSON")
    if "speech_style" in payload and not isinstance(payload["speech_style"], dict):
        raise HTTPException(status_code=400, detail="speech_style 必须是 JSON 对象 {...}")
    if "knowledge_boundary" in payload and isinstance(payload["knowledge_boundary"], str):
        try:
            payload["knowledge_boundary"] = json.loads(payload["knowledge_boundary"])
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="knowledge_boundary 不是合法 JSON")
    if "knowledge_boundary" in payload and not isinstance(payload["knowledge_boundary"], dict):
        raise HTTPException(status_code=400, detail="knowledge_boundary 必须是 JSON 对象 {...}")
    current = engine.persona_card
    if "birthday" in payload:
        try:
            payload["birthday"] = _normalize_birthday(payload["birthday"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "age_tracking" in payload:
        payload["age_tracking"] = bool(payload["age_tracking"])
    tracking = bool(payload.get("age_tracking", current.age_tracking))
    if tracking:
        birthday = str(payload.get("birthday", current.birthday) or "")
        if not birthday:
            raise HTTPException(status_code=400, detail="开启生日自动增长时必须填写生日（MM-DD）")
        try:
            new_age = int(payload.get("age", current.age))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="年龄必须是整数") from exc
        if new_age < 0 or new_age > 200:
            raise HTTPException(status_code=400, detail="年龄超出合理范围")
        changed = (
            not current.age_tracking
            or new_age != current.age
            or birthday != current.birthday
            or not current.age_anchor_date
        )
        payload["age"] = new_age
        payload["birthday"] = birthday
        if changed:
            payload["age_anchor_date"] = date.today().isoformat()
            payload["age_base"] = new_age
    new_entity = bool(body.get("new_entity"))
    engine.persona.save_persona(payload, new_entity=new_entity)
    _reload_engine(request)
    return {"ok": True, "persona": _persona_payload(_engine(request)), "new_entity": new_entity}


@router.get("/prompt")
def get_prompt(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    p = engine.persona_card
    rel = engine.persona.relationship(p.entity_id)
    core = engine.memory.core_facts(p.entity_id)
    template = get_template(engine.settings)
    return {
        "prompt": build_system_prompt(p, rel, core, [], engine.settings.scoring, template=template),
        "template": template,
        "default_template": default_template(),
        "placeholders": PLACEHOLDERS,
        "template_path": str(template_path(engine.settings)),
        "template_meta": template_meta(engine.settings),
        "custom_instructions": p.custom_instructions,
        "file": "pocketmind/prompts.py（只负责渲染，模板在 data/prompt_template.md）",
    }


@router.put("/prompt")
def put_prompt(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    engine = _engine(request)
    if body.get("reset"):
        result = reset_template(engine.settings)
        template = result.get("template", "")
    else:
        template = str(body.get("template") or "")
        result = save_template(engine.settings, template)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "模板不合法")
    p = engine.persona_card
    rel = engine.persona.relationship(p.entity_id)
    core = engine.memory.core_facts(p.entity_id)
    return {
        "ok": True,
        "template": template,
        "template_path": str(template_path(engine.settings)),
        "template_meta": template_meta(engine.settings),
        "prompt": build_system_prompt(p, rel, core, [], engine.settings.scoring, template=template),
        "placeholders": PLACEHOLDERS,
    }


# ---------------------------------------------------------------------------
# 技能池（程序性记忆）
# ---------------------------------------------------------------------------

@router.get("/skills")
def get_skills(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    entity = engine.persona_card.entity_id
    return {
        "skills": engine.skills.list(entity),
        "night_latest": engine.latest_night_run(),
        "dream_latest": engine.latest_dream(),
    }


@router.post("/skills")
def create_skill(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    engine = _engine(request)
    try:
        skill = engine.skills.create(
            engine.persona_card.entity_id,
            name=str(body.get("name") or ""),
            trigger_conditions=str(body.get("trigger_conditions") or ""),
            steps=body.get("steps") or [],
            constraints=str(body.get("constraints") or ""),
            source="manual_admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "skill": skill}


# ---------------------------------------------------------------------------
# 数据浏览 / 导出
# ---------------------------------------------------------------------------

_TABLE_MAP: dict[str, dict[str, Any]] = {
    "memories": {
        "table": "memories", "order": "created_at DESC",
        "search": ["content", "title", "gist"], "drop": ["embedding_json"],
    },
    "events": {"table": "events", "order": "occurred_at DESC", "search": ["summary", "detail"], "drop": []},
    "messages": {"table": "messages", "order": "created_at DESC", "search": ["content"], "drop": ["metadata_json"]},
    "retrieval_logs": {"table": "retrieval_logs", "order": "at DESC", "search": ["query"], "drop": []},
    "relationship_events": {"table": "relationship_events", "order": "at DESC", "search": ["reason"], "drop": []},
    "impressions": {"table": "impressions", "order": "created_at DESC", "search": ["label"], "drop": []},
    "diary_entries": {"table": "diary_entries", "order": "date DESC", "search": ["content"], "drop": []},
    "persona_diaries": {"table": "persona_diaries", "order": "date DESC", "search": ["content"], "drop": []},
    "inner_thoughts": {"table": "inner_thoughts", "order": "at DESC", "search": ["content"], "drop": []},
    "annotations": {"table": "annotations", "order": "created_at DESC", "search": ["notes"], "drop": []},
    "media": {"table": "media", "order": "created_at DESC", "search": ["caption"], "drop": []},
    "core_facts": {"table": "core_facts", "order": "updated_at DESC", "search": ["key", "value"], "drop": []},
    "core_fact_conflicts": {"table": "core_fact_conflicts", "order": "at DESC", "search": ["key", "old_value", "new_value"], "drop": []},
    "skills": {"table": "skills", "order": "updated_at DESC", "search": ["name", "trigger_conditions", "constraints"], "drop": []},
    "night_runs": {"table": "night_runs", "order": "created_at DESC", "search": ["status", "reason", "summary"], "drop": []},
    "dreams": {"table": "dreams", "order": "created_at DESC", "search": ["content", "residue"], "drop": []},
}


def _table_rows(request: Request, table: str, q: str = "", limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    meta = _TABLE_MAP.get(table)
    if not meta:
        raise HTTPException(status_code=404, detail="未知数据表")
    entity_id = _engine(request).persona_card.entity_id
    db: Database = _engine(request).db
    if table == "messages":
        where = ["conversation_id IN (SELECT id FROM conversations WHERE entity_id=?)"]
    else:
        where = ["entity_id=?"]
    params: list[Any] = [entity_id]
    if q and meta["search"]:
        where.append("(" + " OR ".join(f"{col} LIKE ?" for col in meta["search"]) + ")")
        params.extend([f"%{q}%"] * len(meta["search"]))
    sql = f"SELECT * FROM {meta['table']} WHERE {' AND '.join(where)} ORDER BY {meta['order']} LIMIT ? OFFSET ?"
    params.extend([max(1, min(limit, 500)), max(0, offset)])
    rows = db.fetchall(sql, tuple(params))
    time_cols = {"at", "created_at", "updated_at", "occurred_at"}
    for row in rows:
        if table == "retrieval_logs":
            candidates = row.get("candidates_json") or "[]"
            selected = row.get("selected_json") or "[]"
            try:
                cand_list = json.loads(candidates)
                sel_list = json.loads(selected)
            except Exception:
                cand_list, sel_list = [], []
            row["candidates_count"] = len(cand_list)
            row["selected_count"] = len(sel_list)
            row["top_selected"] = (sel_list[0].get("content", "")[:80] if sel_list else "")
            row.pop("candidates_json", None)
            row.pop("selected_json", None)
            meta = dict(meta)
            meta["drop"] = ["candidates_json", "selected_json"]
        for col in meta["drop"]:
            row.pop(col, None)
        for key, value in list(row.items()):
            if key in time_cols and isinstance(value, str) and len(value) >= 16:
                row[key] = value[:16].replace("T", " ")
            elif isinstance(value, str) and len(value) > 4000:
                row[key] = value[:4000] + "…"
    return rows


@router.get("/data_detail")
def get_data_detail(request: Request, kind: str = "memories", id: str = "") -> dict[str, Any]:
    meta = _TABLE_MAP.get(kind)
    if not meta or not id:
        raise HTTPException(status_code=404, detail="未知数据或缺少 id")
    db: Database = _engine(request).db
    row = db.fetchone(f"SELECT * FROM {meta['table']} WHERE id=?", (id,))
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    for col in ("embedding_json",):
        row.pop(col, None)
    return {"kind": kind, "row": row}


@router.get("/data")
def get_data(request: Request, kind: str = "memories", q: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
    if kind == "stats":
        return {"stats": _engine(request).stats()}
    if kind == "relationship":
        p = _engine(request).persona_card
        return {"relationship": _engine(request).persona.relationship(p.entity_id).to_dict()}
    return {"kind": kind, "rows": _table_rows(request, kind, q, limit, offset)}


@router.get("/export")
def export_csv(request: Request, kind: str = "memories", q: str = "") -> Response:
    rows = _table_rows(request, kind, q, limit=5000, offset=0)
    if not rows:
        return Response("没有数据", media_type="text/plain; charset=utf-8")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    data = "\ufeff" + output.getvalue()
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
    )


@router.get("/raw")
def raw_settings(request: Request) -> dict[str, Any]:
    s = _settings(request)
    return {"path": str(s.settings_file), "data": load_raw(s.settings_file)}
