"""SQLite 数据层：建表、连接、通用查询。

为什么用 SQLite：
- 单机、无服务、事务/索引/崩溃恢复都比单 JSON 强；
- 个人规模（万条以内）不需要向量库；
- 数据全在 ./data/pocketmind.db，方便整体备份/迁移。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 9  # 9 = 摄像头视觉观察（只存文本，不存帧）

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_versions (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  persona_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_persona_entity ON persona_versions(entity_id, version);

CREATE TABLE IF NOT EXISTS relationship_state (
  entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  favorability REAL NOT NULL DEFAULT 0,
  trust REAL NOT NULL DEFAULT 0.5,
  closeness REAL NOT NULL DEFAULT 0.3,
  boundary_respect REAL NOT NULL DEFAULT 0.5,
  stage TEXT NOT NULL DEFAULT 'acquaintance',
  mood REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship_events (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  at TEXT NOT NULL,
  field TEXT NOT NULL,
  delta REAL NOT NULL,
  reason TEXT NOT NULL,
  event_id TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  mock INTEGER NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  turn_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  who TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  valence REAL NOT NULL DEFAULT 0,
  severity INTEGER NOT NULL DEFAULT 1,
  importance REAL NOT NULL DEFAULT 0.3,
  base_score REAL NOT NULL DEFAULT 0,
  function TEXT NOT NULL DEFAULT 'social',
  anchor_flag INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0,
  source_quote TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.8,
  interlocutor_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id, occurred_at);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  event_id TEXT,
  layer TEXT NOT NULL DEFAULT 'L2',
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  gist TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'OTHER',
  topic TEXT NOT NULL DEFAULT '',
  function TEXT NOT NULL DEFAULT 'social',
  valence REAL NOT NULL DEFAULT 0,
  severity INTEGER NOT NULL DEFAULT 1,
  importance REAL NOT NULL DEFAULT 0.3,
  base_score REAL NOT NULL DEFAULT 0,
  half_life_days REAL NOT NULL DEFAULT 30,
  distinctiveness REAL NOT NULL DEFAULT 0.5,
  density REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'active',
  retention_stage TEXT NOT NULL DEFAULT 'full',
  pinned INTEGER NOT NULL DEFAULT 0,
  embedding_json TEXT NOT NULL DEFAULT '[]',
  embedding_model TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_recalled_at TEXT NOT NULL DEFAULT '',
  recall_count INTEGER NOT NULL DEFAULT 0,
  used_count INTEGER NOT NULL DEFAULT 0,
  replay_count INTEGER NOT NULL DEFAULT 0,
  superseded_by TEXT NOT NULL DEFAULT '',
  memory_pool TEXT NOT NULL DEFAULT 'declarative',
  reality_status TEXT NOT NULL DEFAULT 'real',
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  consolidation_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_entity_status ON memories(entity_id, status);
CREATE INDEX IF NOT EXISTS idx_memories_topic ON memories(entity_id, category);

CREATE TABLE IF NOT EXISTS core_facts (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  source_event_id TEXT NOT NULL DEFAULT '',
  pinned_by TEXT NOT NULL DEFAULT 'auto',
  valid_from TEXT NOT NULL DEFAULT '',
  valid_to TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 1.0,
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS core_fact_conflicts (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  key TEXT NOT NULL,
  old_id TEXT NOT NULL DEFAULT '',
  new_id TEXT NOT NULL DEFAULT '',
  old_value TEXT NOT NULL DEFAULT '',
  new_value TEXT NOT NULL DEFAULT '',
  resolution TEXT NOT NULL DEFAULT 'new_wins',
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_state (
  entity_id TEXT PRIMARY KEY,
  energy REAL NOT NULL DEFAULT 100,
  sleep_state TEXT NOT NULL DEFAULT 'awake',
  sleep_started_at TEXT NOT NULL DEFAULT '',
  wake_at TEXT NOT NULL DEFAULT '',
  wake_triggered_at TEXT NOT NULL DEFAULT '',
  last_wake_at TEXT NOT NULL DEFAULT '',
  last_consolidated_at TEXT NOT NULL DEFAULT '',
  sleep_backfilled INTEGER NOT NULL DEFAULT 0,
  sleep_duration_hours REAL NOT NULL DEFAULT 0,
  last_update_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unread_messages (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  conversation_id TEXT NOT NULL DEFAULT '',
  answered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dreams (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  sleep_started_at TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  residue TEXT NOT NULL DEFAULT '',
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  model TEXT NOT NULL DEFAULT '',
  mock INTEGER NOT NULL DEFAULT 1,
  trigger_reason TEXT NOT NULL DEFAULT '',
  load_count INTEGER NOT NULL DEFAULT 0,
  load_weight REAL NOT NULL DEFAULT 0,
  mentioned INTEGER NOT NULL DEFAULT 0,
  night_run_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nightly_summaries (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  model TEXT NOT NULL DEFAULT '',
  mock INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vision_observations (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  caption TEXT NOT NULL,
  comment TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  name TEXT NOT NULL,
  trigger_conditions TEXT NOT NULL DEFAULT '',
  steps_json TEXT NOT NULL DEFAULT '[]',
  constraints TEXT NOT NULL DEFAULT '',
  success_count INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT NOT NULL DEFAULT '',
  proficiency REAL NOT NULL DEFAULT 0.6,
  half_life_days REAL NOT NULL DEFAULT 1800,
  floor REAL NOT NULL DEFAULT 0.3,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_events (
  id TEXT PRIMARY KEY,
  skill_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  success INTEGER NOT NULL DEFAULT 1,
  at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS night_runs (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  sleep_started_at TEXT NOT NULL DEFAULT '',
  sleep_duration_hours REAL NOT NULL DEFAULT 0,
  triggered_by TEXT NOT NULL DEFAULT '',
  backfilled INTEGER NOT NULL DEFAULT 0,
  window_start TEXT NOT NULL DEFAULT '',
  window_end TEXT NOT NULL DEFAULT '',
  consolidated_count INTEGER NOT NULL DEFAULT 0,
  consolidated_weight REAL NOT NULL DEFAULT 0,
  important_count INTEGER NOT NULL DEFAULT 0,
  skill_count INTEGER NOT NULL DEFAULT 0,
  skill_maintained INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  dream_id TEXT NOT NULL DEFAULT '',
  memory_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impressions (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  event_id TEXT,
  label TEXT NOT NULL,
  polarity TEXT NOT NULL DEFAULT 'negative',
  strength_base REAL NOT NULL DEFAULT 0.5,
  strength_current REAL NOT NULL DEFAULT 0.5,
  floor REAL NOT NULL DEFAULT 0.2,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diary_entries (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  date TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(entity_id, date)
);

CREATE TABLE IF NOT EXISTS persona_diaries (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  date TEXT NOT NULL,
  content TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(entity_id, date)
);

CREATE TABLE IF NOT EXISTS inner_thoughts (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  at TEXT NOT NULL,
  content TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'dev'
);

CREATE TABLE IF NOT EXISTS retrieval_logs (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  at TEXT NOT NULL,
  query TEXT NOT NULL,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  selected_json TEXT NOT NULL DEFAULT '[]',
  method TEXT NOT NULL DEFAULT 'hybrid',
  weights_version TEXT NOT NULL DEFAULT '',
  embedding_model TEXT NOT NULL DEFAULT '',
  gate TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS recall_logs (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  at TEXT NOT NULL,
  target_type TEXT NOT NULL DEFAULT 'memory',
  transient_reward REAL NOT NULL DEFAULT 0,
  trigger_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS forget_events (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  at TEXT NOT NULL,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  retention REAL NOT NULL,
  reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS api_calls (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  ok INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS annotations (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  annotator TEXT NOT NULL DEFAULT 'user',
  scores_json TEXT NOT NULL DEFAULT '{}',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  path TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  caption TEXT NOT NULL DEFAULT '',
  memory_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_entity ON media(entity_id, created_at);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  memory_id UNINDEXED,
  tokens,
  tokenize='unicode61'
);
"""


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:16]
    return f"{prefix}_{raw}" if prefix else raw


class Database:
    """轻量数据库封装；每次操作独立连接，避免 FastAPI 线程问题。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def get_meta(self, key: str, default: str = "") -> str:
        if not self.path.exists():
            return default
        try:
            conn = self.connect()
            try:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return str(row["value"]) if row else default
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def schema_version(self) -> int:
        try:
            return int(self.get_meta("schema_version", "0") or 0)
        except ValueError:
            return 0

    def _backup_before_migration(self, old_version: int) -> str:
        """迁移前用 SQLite backup API 做一致性备份；失败不阻塞但记录在返回值。"""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return ""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / f"pre-migrate-v{old_version}-{stamp}.db"
        try:
            src = sqlite3.connect(self.path)
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            return str(dest)
        except Exception:
            return ""

    def init_schema(self) -> None:
        old_version = self.schema_version()
        if old_version < SCHEMA_VERSION:
            backup = self._backup_before_migration(old_version)
            if backup:
                try:
                    self.set_meta("last_pre_migrate_backup", backup)
                except Exception:
                    pass
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            try:
                conn.executescript(FTS_SCHEMA)
            except sqlite3.OperationalError:
                # 某些环境未编译 FTS5：保留关键词兜底，不让初始化失败
                pass
            self._migrate(conn)
            now = now_iso()
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                """INSERT OR IGNORE INTO schema_migrations(version,applied_at,description)
                   VALUES(?,?,?)""",
                (
                    SCHEMA_VERSION,
                    now,
                    (
                        "A6/B baseline + C2/C3/C4/C5: core_facts v2, persona_diaries source_ids, "
                        "context budgets, agent_state, unread_messages, dreams, nightly_summaries"
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """极简迁移：只补缺失列，不动已有数据（M1 迭代期用）。"""
        def cols(table: str) -> set[str]:
            try:
                return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.OperationalError:
                return set()

        memory_cols = cols("memories")
        if memory_cols:
            if "topic" not in memory_cols:
                conn.execute("ALTER TABLE memories ADD COLUMN topic TEXT NOT NULL DEFAULT ''")
            if "retention_stage" not in memory_cols:
                conn.execute("ALTER TABLE memories ADD COLUMN retention_stage TEXT NOT NULL DEFAULT 'full'")
            for col, ddl in (
                ("memory_pool", "TEXT NOT NULL DEFAULT 'declarative'"),
                ("reality_status", "TEXT NOT NULL DEFAULT 'real'"),
                ("source_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("consolidation_key", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in memory_cols:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
        log_cols = cols("retrieval_logs")
        if log_cols and "gate" not in log_cols:
            conn.execute("ALTER TABLE retrieval_logs ADD COLUMN gate TEXT NOT NULL DEFAULT ''")
        event_cols = cols("events")
        if event_cols and "interlocutor_json" not in event_cols:
            conn.execute("ALTER TABLE events ADD COLUMN interlocutor_json TEXT NOT NULL DEFAULT '{}'")
        msg_cols = cols("messages")
        if msg_cols and "turn_id" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")
        diary_cols = cols("persona_diaries")
        if diary_cols and "source_ids_json" not in diary_cols:
            conn.execute("ALTER TABLE persona_diaries ADD COLUMN source_ids_json TEXT NOT NULL DEFAULT '[]'")
        core_cols = cols("core_facts")
        if core_cols and "valid_from" not in core_cols:
            # core_facts v1（唯一 key + 直接覆盖）→ v2（有效期/历史/置信度）。
            # 旧表改名为 core_facts_legacy_v1 保留，不删数据；新表复制为 active。
            conn.execute("ALTER TABLE core_facts RENAME TO core_facts_legacy_v1")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS core_facts (
                  id TEXT PRIMARY KEY,
                  entity_id TEXT NOT NULL,
                  key TEXT NOT NULL,
                  value TEXT NOT NULL,
                  source_event_id TEXT NOT NULL DEFAULT '',
                  pinned_by TEXT NOT NULL DEFAULT 'auto',
                  valid_from TEXT NOT NULL DEFAULT '',
                  valid_to TEXT NOT NULL DEFAULT '',
                  confidence REAL NOT NULL DEFAULT 1.0,
                  status TEXT NOT NULL DEFAULT 'active',
                  superseded_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT INTO core_facts
                   (id,entity_id,key,value,source_event_id,pinned_by,valid_from,valid_to,
                    confidence,status,superseded_by,created_at,updated_at)
                   SELECT id,entity_id,key,value,source_event_id,pinned_by,updated_at,'',
                          1.0,'active','',updated_at,updated_at
                   FROM core_facts_legacy_v1"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_core_facts_active ON core_facts(entity_id,key,status)"
            )
        if core_cols and "valid_from" in core_cols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_core_facts_active ON core_facts(entity_id,key,status)"
            )
        unread_cols = cols("unread_messages")
        if unread_cols:
            if "conversation_id" not in unread_cols:
                conn.execute("ALTER TABLE unread_messages ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
            if "answered" not in unread_cols:
                conn.execute("ALTER TABLE unread_messages ADD COLUMN answered INTEGER NOT NULL DEFAULT 0")
        dream_cols = cols("dreams")
        if dream_cols:
            if "trigger_reason" not in dream_cols:
                conn.execute("ALTER TABLE dreams ADD COLUMN trigger_reason TEXT NOT NULL DEFAULT ''")
            if "load_count" not in dream_cols:
                conn.execute("ALTER TABLE dreams ADD COLUMN load_count INTEGER NOT NULL DEFAULT 0")
            if "load_weight" not in dream_cols:
                conn.execute("ALTER TABLE dreams ADD COLUMN load_weight REAL NOT NULL DEFAULT 0")
            if "mentioned" not in dream_cols:
                conn.execute("ALTER TABLE dreams ADD COLUMN mentioned INTEGER NOT NULL DEFAULT 0")
            if "night_run_id" not in dream_cols:
                conn.execute("ALTER TABLE dreams ADD COLUMN night_run_id TEXT NOT NULL DEFAULT ''")
        state_cols = cols("agent_state")
        if state_cols:
            for col, ddl in (
                ("wake_triggered_at", "TEXT NOT NULL DEFAULT ''"),
                ("last_wake_at", "TEXT NOT NULL DEFAULT ''"),
                ("last_consolidated_at", "TEXT NOT NULL DEFAULT ''"),
                ("sleep_backfilled", "INTEGER NOT NULL DEFAULT 0"),
                ("sleep_duration_hours", "REAL NOT NULL DEFAULT 0"),
            ):
                if col not in state_cols:
                    conn.execute(f"ALTER TABLE agent_state ADD COLUMN {col} {ddl}")
        run_cols = cols("night_runs")
        if run_cols:
            for col, ddl in (
                ("sleep_duration_hours", "REAL NOT NULL DEFAULT 0"),
                ("triggered_by", "TEXT NOT NULL DEFAULT ''"),
                ("backfilled", "INTEGER NOT NULL DEFAULT 0"),
                ("memory_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in run_cols:
                    conn.execute(f"ALTER TABLE night_runs ADD COLUMN {col} {ddl}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def fts_available(self) -> bool:
        try:
            conn = self.connect()
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
            conn.execute("DROP TABLE IF EXISTS _fts_probe")
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False
