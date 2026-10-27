"""实体/人格卡/关系状态。"""

from __future__ import annotations

from datetime import date

from .config import DEFAULT_PERSONA, Settings
from .db import Database, new_id, now_iso
from .models import PersonaCard, RelationshipState
from .scoring import update_relationship


class PersonaService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------
    # 实体/人格
    # ------------------------------------------------------------------
    def ensure_entity(self) -> PersonaCard:
        meta = self.db.fetchone("SELECT value FROM meta WHERE key='default_entity_id'")
        if meta:
            persona = self.get_current_persona(meta["value"])
            if persona:
                return persona
        return self.create_entity(DEFAULT_PERSONA)

    def create_entity(self, persona_data: dict | PersonaCard) -> PersonaCard:
        """新设定 = 新实体（角色隔离）；旧实体保留但不再使用。"""
        if isinstance(persona_data, PersonaCard):
            payload = persona_data.to_dict()
        else:
            payload = dict(persona_data)
        entity_id = new_id("ent")
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO entities(id,name,status,created_at) VALUES(?,?,?,?)",
                (entity_id, str(payload.get("name", "小满")), "active", now),
            )
            persona = PersonaCard(
                entity_id=entity_id,
                name=str(payload.get("name", "小满")),
                age=int(payload.get("age", 7)),
                gender=str(payload.get("gender", "女")),
                species=str(payload.get("species", "人类小孩（设定）")),
                origin=str(payload.get("origin", "created")),
                values=list(payload.get("values") or []),
                traits=list(payload.get("traits") or []),
                speech_style=dict(payload.get("speech_style") or {}),
                boundaries=list(payload.get("boundaries") or []),
                knowledge_boundary=dict(payload.get("knowledge_boundary") or {}),
                origin_story=str(payload.get("origin_story", "")),
                custom_instructions=str(payload.get("custom_instructions", "")),
                age_tracking=bool(payload.get("age_tracking", False)),
                birthday=str(payload.get("birthday", "")),
                age_anchor_date=str(payload.get("age_anchor_date") or (
                    date.today().isoformat() if payload.get("age_tracking") and payload.get("birthday") else ""
                )),
                age_base=int(payload.get("age_base", 0) or (
                    int(payload.get("age", 7)) if payload.get("age_tracking") and payload.get("birthday") else 0
                )),
                version=1,
            )
            conn.execute(
                "INSERT INTO persona_versions(id,entity_id,version,persona_json,created_at,is_current) VALUES(?,?,?,?,?,1)",
                (new_id("pv"), entity_id, 1, persona.to_json(), now),
            )
            conn.execute(
                "INSERT INTO relationship_state(entity_id,updated_at) VALUES(?,?)",
                (entity_id, now),
            )
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('default_entity_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (entity_id,),
            )
        return persona

    def get_current_persona(self, entity_id: str | None = None) -> PersonaCard | None:
        eid = entity_id or self.default_entity_id()
        if not eid:
            return None
        row = self.db.fetchone(
            "SELECT persona_json FROM persona_versions WHERE entity_id=? AND is_current=1 ORDER BY version DESC LIMIT 1",
            (eid,),
        )
        if not row:
            return None
        return PersonaCard(**__import__("json").loads(row["persona_json"]))

    def save_persona(self, payload: dict, new_entity: bool = False) -> PersonaCard:
        """保存人格修改。

        new_entity=False：当前实体新版本（保留记忆；适合改语气/补充指令）；
        new_entity=True：新建实体（角色隔离，记忆从零；换年龄/性别/物种建议用这个）。
        """
        if new_entity:
            return self.create_entity(payload)
        current = self.get_current_persona()
        if not current:
            return self.create_entity(payload)
        now = now_iso()
        data = current.to_dict()
        data.update({k: v for k, v in payload.items() if v is not None})
        # 不可通过普通保存改 entity_id / version
        data["entity_id"] = current.entity_id
        data["version"] = current.version + 1
        persona = PersonaCard(**data)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE persona_versions SET is_current=0 WHERE entity_id=?",
                (current.entity_id,),
            )
            conn.execute(
                "INSERT INTO persona_versions(id,entity_id,version,persona_json,created_at,is_current) VALUES(?,?,?,?,?,1)",
                (new_id("pv"), current.entity_id, persona.version, persona.to_json(), now),
            )
            conn.execute(
                "UPDATE entities SET name=? WHERE id=?",
                (persona.name, current.entity_id),
            )
        return persona

    def default_entity_id(self) -> str:
        meta = self.db.fetchone("SELECT value FROM meta WHERE key='default_entity_id'")
        return meta["value"] if meta else ""

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------
    def relationship(self, entity_id: str) -> RelationshipState:
        row = self.db.fetchone("SELECT * FROM relationship_state WHERE entity_id=?", (entity_id,))
        if not row:
            now = now_iso()
            self.db.execute("INSERT INTO relationship_state(entity_id,updated_at) VALUES(?,?)", (entity_id, now))
            return RelationshipState(entity_id=entity_id, updated_at=now)
        return RelationshipState(
            entity_id=row["entity_id"],
            favorability=float(row["favorability"]),
            trust=float(row["trust"]),
            closeness=float(row["closeness"]),
            boundary_respect=float(row["boundary_respect"]),
            stage=row["stage"],
            mood=float(row["mood"]),
            updated_at=row["updated_at"],
        )

    def apply_event_to_relationship(
        self, state: RelationshipState, base: float, importance: float, event_type: str
    ) -> RelationshipState:
        new_state, changes = update_relationship(state, base, importance, self.settings.scoring, event_type)
        if not changes:
            return state
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE relationship_state SET favorability=?, trust=?, closeness=?, boundary_respect=?, stage=?, mood=?, updated_at=? WHERE entity_id=?",
                (
                    new_state.favorability,
                    new_state.trust,
                    new_state.closeness,
                    new_state.boundary_respect,
                    new_state.stage,
                    new_state.mood,
                    now,
                    state.entity_id,
                ),
            )
            for change in changes:
                conn.execute(
                    "INSERT INTO relationship_events(id,entity_id,at,field,delta,reason,event_id) VALUES(?,?,?,?,?,?,?)",
                    (new_id("rel"), state.entity_id, now, change["field"], change["delta"], change["reason"], ""),
                )
        new_state.updated_at = now
        return new_state

    def update_stage(self, entity_id: str, stage: str) -> None:
        self.db.execute(
            "UPDATE relationship_state SET stage=?, updated_at=? WHERE entity_id=?",
            (stage, now_iso(), entity_id),
        )
