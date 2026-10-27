"""配置：环境变量 + config/scoring.yaml。

设计原则（对应工作区规范）：
- 用户常改的参数集中在这里；代码里不散落魔法数字；
- 路径全部相对项目根目录，数据默认在 ./data，方便整体备份/迁移；
- 没有 API Key 时自动进入 mock 模式，保证离线也能跑通与测试；
- 全 API 模式：M1 不下载本地模型，embedding 也走 API；无 Key 时退化为 mock 向量。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 开发时：ROOT=项目根；打包成 EXE 后：ROOT=exe 所在目录（数据/配置放旁边）。
PACKAGE_ROOT = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else PACKAGE_ROOT.parent
# 只读资源（模板/config/示例）在开发时=ROOT，PyInstaller 打包后=_MEIPASS。
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
load_dotenv(ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass
class ScoringConfig:
    """打分/检索/遗忘的默认参数。改这里或 config/scoring.yaml 即可。"""

    # ---- 检索权重（最终排序 = 加权和）----
    w_sem: float = 0.45
    w_aff: float = 0.20
    w_imp: float = 0.15
    w_rec: float = 0.10
    w_rel: float = 0.10

    # ---- 检索 ----
    recall_limit: int = 5
    candidate_limit: int = 30
    vector_min_score: float = 0.35        # 向量召回最低分
    final_min_score: float = 0.30         # 注入最低综合分
    keyvec_dim: int = 64                  # mock 向量维度

    # ---- 记忆门（闲聊少查 / 显式线索必查）----
    gate_casual_limit: int = 1            # 闲聊最多注入 1 条
    gate_normal_limit: int = 3            # 普通陈述最多注入 3 条
    gate_casual_max_len: int = 12         # 短句视为闲聊
    gate_explicit_cues: list[str] = field(
        default_factory=lambda: [
            "记得", "还记得", "上次", "那天", "之前", "以前", "答应", "承诺",
            "生日", "叫什么", "名字", "住哪", "住在哪", "哪里", "喜欢什么",
            "喜欢吃什么", "小时候", "第一次", "我们以前", "考试结果",
        ]
    )

    # ---- 去重/冲突 ----
    dedup_threshold: float = 0.55
    noise_min_len: int = 4
    noise_max_len: int = 400
    max_new_per_turn: int = 8

    # ---- 事件打分 ----
    base_score_scale: float = 10.0        # R_base = scale * valence * severity_weight
    severity_weights: dict[int, float] = field(
        default_factory=lambda: {1: 0.3, 2: 0.6, 3: 1.0}
    )

    # ---- 半衰期（天）----
    half_life_core: float = 36500.0
    half_life_constraint: float = 3650.0
    half_life_anchor: float = 3650.0
    half_life_important_positive: float = 180.0
    half_life_episodic: float = 30.0
    half_life_minor_negative: float = 14.0
    half_life_severe_negative: float = 90.0
    half_life_low_info: float = 3.0
    half_life_impression: float = 3650.0
    impression_floor: float = 0.20

    # ---- 遗忘/降级 ----
    forget_archive_below: float = 0.05
    forget_gist_below: float = 0.20
    forget_summary_below: float = 0.50

    # ---- 关系 ----
    favorability_alpha: float = 0.10
    favorability_event_cap: float = 3.0
    trust_alpha: float = 0.02

    # ---- 回忆/预期奖励 ----
    recall_k: float = 0.50
    recall_lambda: float = 0.20
    recall_tau_minutes: float = 30.0
    anticipation_p_min: float = 0.05
    anticipation_p_max: float = 0.95
    disappointment_penalty: float = 0.20

    # 候选池：最近 N ∪ 高重要度/高情绪/pin；避免远期重要记忆被挤出
    candidate_recent_limit: int = 300
    candidate_priority_limit: int = 100

    # 回忆加固：每次被选中使用后，向最大半衰期渐进延长（不无限增长）
    recall_half_life_gain: float = 0.01
    recall_half_life_max: float = 3650.0

    # ---- 注入预算（字符数，中文场景近似 token*1.5）----
    # 模型上下文窗口远大于当前用量；这里限制的是“每轮注入多少”，不是模型容量。
    context_budget_chars: int = 4000
    context_safety_tail_chars: int = 900
    recent_messages: int = 200
    recent_context_budget_chars: int = 8000
    # 最近消息时间窗（小时）；0=不按时间过滤。72=最近3天，避免无限回传旧聊天。
    recent_window_hours: int = 72

    @classmethod
    def load(cls, path: Path | None = None) -> ScoringConfig:
        cfg = cls()
        yaml_path = path or (RESOURCE_ROOT / "config" / "scoring.yaml")
        if not yaml_path.exists():
            yaml_path = ROOT / "config" / "scoring.yaml"
        if yaml_path.exists():
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            for f in fields(cls):
                if f.name in data and data[f.name] is not None:
                    value = data[f.name]
                    # YAML 的 severity_weights 键是字符串，转回 int
                    if f.name == "severity_weights":
                        value = {int(k): float(v) for k, v in value.items()}
                    setattr(cfg, f.name, value)
        return cfg


@dataclass
class Settings:
    """全局设置：路径、模型服务、mock 开关。"""

    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: Path(_env("POCKETMIND_DATA_DIR", str(ROOT / "data"))))
    db_path: Path = field(init=False)
    diary_dir: Path = field(init=False)
    log_dir: Path = field(init=False)
    settings_file: Path = field(init=False)
    secret_store: Any = field(init=False, default=None)
    secret_store_error: str = field(init=False, default="")

    # LLM（主对话 + 记忆抽取；同供应商，默认 DeepSeek）
    llm_provider: str = field(default_factory=lambda: _env("POCKETMIND_LLM_PROVIDER", "deepseek"))
    llm_api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    llm_model: str = field(default_factory=lambda: _env("POCKETMIND_MAIN_MODEL", "deepseek-flash"))
    thinking: str = field(default_factory=lambda: _env("POCKETMIND_THINKING", "disabled"))
    llm_temperature: float = field(default_factory=lambda: float(_env("POCKETMIND_TEMPERATURE", "0.85") or 0.85))
    llm_max_tokens: int = field(default_factory=lambda: int(_env("POCKETMIND_MAX_TOKENS", "320") or 320))

    # Embedding（全 API；无 Key 时 mock 退化，保证离线可跑）
    embedding_provider: str = field(default_factory=lambda: _env("POCKETMIND_EMBEDDING_PROVIDER", "qwen"))
    embedding_api_key: str = field(default_factory=lambda: _env("QWEN_API_KEY", "") or _env("SILICONFLOW_API_KEY", ""))
    embedding_base_url: str = field(
        default_factory=lambda: _env("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    )
    embedding_model: str = field(default_factory=lambda: _env("QWEN_EMBEDDING_MODEL", "text-embedding-v4"))
    embedding_dim_hint: int = field(default_factory=lambda: int(_env("POCKETMIND_EMBEDDING_DIM", "0") or 0))

    log_level: str = field(default_factory=lambda: _env("POCKETMIND_LOG_LEVEL", "INFO"))
    diary_trigger_hour: int = field(default_factory=lambda: int(_env("POCKETMIND_DIARY_TRIGGER_HOUR", "23") or 23))
    diary_scheduler: bool = field(default_factory=lambda: _env_bool("POCKETMIND_DIARY_SCHEDULER", True))
    # C2/C3 精力/睡眠（真实时间；关闭时按 last_update_at 结算）
    energy_enabled: bool = field(default_factory=lambda: _env_bool("POCKETMIND_ENERGY_ENABLED", True))
    energy_base_drain_per_hour: float = field(default_factory=lambda: float(_env("POCKETMIND_ENERGY_BASE_DRAIN", "2.5") or 2.5))
    energy_emotion_scale: float = field(default_factory=lambda: float(_env("POCKETMIND_ENERGY_EMOTION_SCALE", "0.3") or 0.3))
    energy_positive_factor: float = field(default_factory=lambda: float(_env("POCKETMIND_ENERGY_POSITIVE_FACTOR", "0.8") or 0.8))
    energy_negative_factor: float = field(default_factory=lambda: float(_env("POCKETMIND_ENERGY_NEGATIVE_FACTOR", "1.2") or 1.2))
    energy_sleep_hours: float = field(default_factory=lambda: float(_env("POCKETMIND_ENERGY_SLEEP_HOURS", "8") or 8))
    energy_recover_per_hour: float = field(default_factory=lambda: float(_env("POCKETMIND_ENERGY_RECOVER_PER_HOUR", "12.5") or 12.5))
    # 关机补录/短睡眠唤醒
    sleep_backfill_min_minutes: int = field(default_factory=lambda: int(_env("POCKETMIND_SLEEP_BACKFILL_MIN_MINUTES", "30") or 30))
    short_sleep_wake_probability: float = field(default_factory=lambda: float(_env("POCKETMIND_SHORT_SLEEP_WAKE_PROB", "0.35") or 0.35))
    wake_greeting_enabled: bool = field(default_factory=lambda: _env_bool("POCKETMIND_WAKE_GREETING", True))
    # 摄像头观察接入主对话：最近一次视觉观察的有效期（秒）
    vision_context_seconds: int = field(default_factory=lambda: int(_env("POCKETMIND_VISION_CONTEXT_SECONDS", "300") or 300))
    # 摄像头评论写入主对话的最小间隔（秒），避免刷屏
    vision_chat_min_seconds: int = field(default_factory=lambda: int(_env("POCKETMIND_VISION_CHAT_MIN_SECONDS", "20") or 20))
    # 写回式巩固：夜间摘要作为“摘要记忆”写回普通记忆池（不是新事实）
    consolidation_writeback_enabled: bool = field(default_factory=lambda: _env_bool("POCKETMIND_CONSOLIDATION_WRITEBACK", True))
    consolidation_summary_importance: float = field(default_factory=lambda: float(_env("POCKETMIND_CONSOLIDATION_IMPORTANCE", "0.25") or 0.25))
    consolidation_summary_half_life_days: float = field(default_factory=lambda: float(_env("POCKETMIND_CONSOLIDATION_HALF_LIFE", "45") or 45))
    # C5 梦触发：不是每天做；当天内容够多/够重要才触发，且有冷却
    dream_min_events: int = field(default_factory=lambda: int(_env("POCKETMIND_DREAM_MIN_EVENTS", "8") or 8))
    dream_min_weight: float = field(default_factory=lambda: float(_env("POCKETMIND_DREAM_MIN_WEIGHT", "5.0") or 5.0))
    dream_min_important: int = field(default_factory=lambda: int(_env("POCKETMIND_DREAM_MIN_IMPORTANT", "1") or 1))
    dream_cooldown_hours: float = field(default_factory=lambda: float(_env("POCKETMIND_DREAM_COOLDOWN_HOURS", "36") or 36))
    dream_window_hours: float = field(default_factory=lambda: float(_env("POCKETMIND_DREAM_WINDOW_HOURS", "24") or 24))
    # M2a 技能池
    skill_default_proficiency: float = field(default_factory=lambda: float(_env("POCKETMIND_SKILL_PROFICIENCY", "0.6") or 0.6))
    skill_proficiency_gain: float = field(default_factory=lambda: float(_env("POCKETMIND_SKILL_GAIN", "0.25") or 0.25))
    skill_default_half_life_days: float = field(default_factory=lambda: float(_env("POCKETMIND_SKILL_HALF_LIFE", "1800") or 1800))
    skill_default_floor: float = field(default_factory=lambda: float(_env("POCKETMIND_SKILL_FLOOR", "0.3") or 0.3))
    skill_inject_limit: int = field(default_factory=lambda: int(_env("POCKETMIND_SKILL_INJECT_LIMIT", "2") or 2))
    mock: bool = field(default_factory=lambda: _env_bool("POCKETMIND_MOCK", _env("DEEPSEEK_API_KEY", "") == ""))

    scoring: ScoringConfig = field(default_factory=ScoringConfig.load)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "pocketmind.db"
        self.diary_dir = self.data_dir / "diary"
        self.log_dir = self.data_dir / "logs"
        self.settings_file = self.data_dir / "settings.json"
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # API Key 加密存储（AES-256-GCM + Windows DPAPI）；旧明文在 _apply_settings_file 中迁移。
        try:
            from .secrets_store import SecretStore

            self.secret_store = SecretStore(self.data_dir)
        except Exception as exc:
            self.secret_store = None
            self.secret_store_error = f"加密存储初始化失败：{exc}"
        self._apply_settings_file()
        if not _env("POCKETMIND_MOCK", ""):
            self.mock = not bool(self.llm_api_key)

    def _apply_settings_file(self) -> None:
        """把 data/settings.json（管理台保存）覆盖到环境变量结果上。

        优先级：settings.json > 环境变量 > 默认值。
        这样用户可以在 UI 里改 API/模型/参数，不必手写 .env；
        settings.json 在 data/ 下，已被 .gitignore 排除。
        """
        try:
            if not self.settings_file.exists():
                return
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except Exception:
            return
        llm = data.get("llm") or {}
        emb = data.get("embedding") or {}
        for key, attr in (
            ("provider", "llm_provider"),
            ("base_url", "llm_base_url"),
            ("model", "llm_model"),
            ("thinking", "thinking"),
        ):
            if llm.get(key) not in (None, ""):
                setattr(self, attr, str(llm[key]))
        for key, attr, cast in (
            ("temperature", "llm_temperature", float),
            ("max_tokens", "llm_max_tokens", int),
        ):
            if llm.get(key) not in (None, ""):
                try:
                    setattr(self, attr, cast(llm[key]))
                except (TypeError, ValueError):
                    pass
        for key, attr in (
            ("provider", "embedding_provider"),
            ("base_url", "embedding_base_url"),
            ("model", "embedding_model"),
        ):
            if emb.get(key) not in (None, ""):
                setattr(self, attr, str(emb[key]))
        if emb.get("dim") not in (None, ""):
            try:
                self.embedding_dim_hint = int(emb["dim"])
            except (TypeError, ValueError):
                pass
        scoring = data.get("scoring") or {}
        if scoring:
            # 旧版设置只写过 context_budget_chars=2400 / recent_messages=10，
            # 且没有时间窗字段。用户升级后应使用新的“最近 3 天”预算；
            # 只在值仍是旧默认值时迁移，保留用户自己改过的值。
            if "recent_window_hours" not in scoring and scoring.get("context_budget_chars") == 2400:
                scoring.pop("context_budget_chars", None)
            if "recent_context_budget_chars" not in scoring and scoring.get("recent_messages") == 10:
                scoring.pop("recent_messages", None)
            valid = {f.name for f in fields(ScoringConfig)}
            for key, value in scoring.items():
                if key in valid and value is not None:
                    try:
                        if key == "severity_weights":
                            value = {int(k): float(v) for k, v in dict(value).items()}
                        setattr(self.scoring, key, value)
                    except Exception:
                        pass
        self._migrate_plaintext_keys(data)

    def _migrate_plaintext_keys(self, data: dict[str, Any]) -> None:
        """把旧 settings.json 里的明文 api_key 迁到加密表；明文不留在 JSON。"""
        changed = False
        cleared: set[str] = set()
        for section, secret_name, attr in (
            ("llm", "llm", "llm_api_key"),
            ("embedding", "embedding", "embedding_api_key"),
        ):
            section_data = data.get(section)
            if not isinstance(section_data, dict) or "api_key" not in section_data:
                continue
            plain = str(section_data.get("api_key") or "").strip()
            if not plain:
                # 旧 UI 的“清空 Key”语义：明确压制环境变量
                setattr(self, attr, "")
                cleared.add(secret_name)
                continue
            if self.secret_store is None:
                setattr(self, attr, plain)
                self.secret_store_error = "加密存储不可用，旧明文 Key 暂未迁移"
                continue
            try:
                self.secret_store.put(secret_name, plain)
                if self.secret_store.get(secret_name) != plain:
                    raise RuntimeError("加密写入后回读不一致")
                section_data.pop("api_key", None)
                changed = True
            except Exception as exc:
                self.secret_store_error = f"API Key 迁移失败：{exc}"
                # 迁移失败时保留明文可用，但明确告警；下次启动重试
                setattr(self, attr, plain)
        if changed:
            try:
                from .settings_store import save_raw

                save_raw(self.settings_file, data)
            except Exception as exc:
                self.secret_store_error = f"加密后清理 settings.json 失败：{exc}"
        if self.secret_store is None:
            return
        try:
            secrets = self.secret_store.load_all()
        except Exception as exc:
            self.secret_store_error = f"读取加密 Key 失败：{exc}"
            return
        for secret_name, attr in (("llm", "llm_api_key"), ("embedding", "embedding_api_key")):
            # 明文里显式空值表示“清空/压制”，此时不再加载加密值
            if secret_name in cleared:
                continue
            if secrets.get(secret_name):
                setattr(self, attr, secrets[secret_name])

    def secret_status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "backend": "AES-256-GCM + Windows DPAPI",
            "directory": str(self.data_dir / "secrets"),
            "available": self.secret_store is not None,
            "error": self.secret_store_error,
            "llm_key_present": False,
            "embedding_key_present": False,
        }
        if self.secret_store is None:
            return info
        try:
            live = self.secret_store.status()
            info.update(live)
            info["error"] = self.secret_store_error or str(live.get("error", ""))
        except Exception as exc:
            info["error"] = str(exc)
        return info

    @property
    def use_real_llm(self) -> bool:
        return (not self.mock) and bool(self.llm_api_key)


DEFAULT_PERSONA: dict[str, Any] = {
    "name": "小满",
    "age": 7,
    "gender": "女",
    "species": "人类小孩（设定）",
    "origin": "created",  # 关键：被创造的智能生命，不是自然人；被问要如实说
    "values": ["不替你做决定", "答应的事要做到", "不懂就问，不装懂"],
    "traits": ["慢热", "嘴硬心软", "对细节敏感", "好奇"],
    "speech_style": {
        "sentence_length": "短句为主，偶尔长句",
        "particles": ["呀", "呢", "嘛", "诶"],
        "forbidden": ["亲", "宝子", "作为AI", "根据我的记忆"],
        "address_default": "你",
        "emoji_rate": "低（每 10 轮 0~3 个）",
    },
    "boundaries": [
        "不假装自己是自然人；被问身份如实说明是被创造出来的",
        "不编造记忆；不确定就说记不清",
        "不替用户做重大决定",
        "不使用排他/依赖式话术",
    ],
    "knowledge_boundary": {
        "known_domains": ["动画", "动物", "学校", "公园", "零食", "游戏", "家里的人"],
        "unknown_domains": ["线性代数", "相对论", "政治", "金融", "成人话题"],
        "curiosity_style": "不懂就好奇地问，或说『我们一起查查看？』",
        "vocab_level": "具体词、短句；少抽象名词",
    },
    "origin_story": "我是被做出来的，住在这台电脑里；第一次开机时，你还在改一个总崩的项目。",
}
