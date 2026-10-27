from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.release_security import compute_source_hash
from pocketmind.secrets_store import SecretStore, SecretStoreError
from pocketmind.web import create_app


def test_secret_store_aes_gcm_roundtrip_and_tamper(tmp_path: Path) -> None:
    key = b"k" * 32
    store = SecretStore(tmp_path / "data", _test_key=key)
    store.save_all({"llm": "sk-demo-123", "embedding": "emb-demo-456"})
    assert store.load_all() == {"llm": "sk-demo-123", "embedding": "emb-demo-456"}
    raw = (tmp_path / "data" / "secrets" / "api_keys.enc").read_bytes()
    assert b"sk-demo-123" not in raw
    # 相同明文两次加密应不同（随机 IV）
    first = (tmp_path / "data" / "secrets" / "api_keys.enc").read_text(encoding="utf-8")
    store.save_all({"llm": "sk-demo-123", "embedding": "emb-demo-456"})
    second = (tmp_path / "data" / "secrets" / "api_keys.enc").read_text(encoding="utf-8")
    assert first != second
    # 篡改必须失败
    f = tmp_path / "data" / "secrets" / "api_keys.enc"
    data = bytearray(f.read_bytes())
    data[-1] = 48 if data[-1] != 48 else 49
    f.write_bytes(bytes(data))
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "data", _test_key=key).load_all()


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI 仅 Windows")
def test_settings_migrates_plaintext_key_to_encrypted(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(
        json.dumps({
            "llm": {"provider": "deepseek", "model": "demo", "api_key": "sk-plaintext-demo"},
            "embedding": {"api_key": "emb-plaintext-demo"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    s = Settings(data_dir=data, mock=False)
    assert s.llm_api_key == "sk-plaintext-demo"
    assert s.embedding_api_key == "emb-plaintext-demo"
    assert s.secret_status()["llm_key_present"] is True
    raw = (data / "settings.json").read_text(encoding="utf-8")
    assert "sk-plaintext-demo" not in raw
    assert "emb-plaintext-demo" not in raw
    assert (data / "secrets" / "master.key.dpapi").exists()
    assert (data / "secrets" / "api_keys.enc").exists()
    s2 = Settings(data_dir=data, mock=False)
    assert s2.llm_api_key == "sk-plaintext-demo"
    assert s2.embedding_api_key == "emb-plaintext-demo"


def test_admin_saves_key_encrypted(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", mock=True))
    client = TestClient(app)
    resp = client.put(
        "/api/admin/settings",
        json={"llm": {"provider": "deepseek", "api_key": "sk-admin-test-9876"}},
    )
    assert resp.status_code == 200
    raw = (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")
    assert "sk-admin-test-9876" not in raw
    status = client.get("/api/admin/settings").json()
    assert status["llm"]["api_key_encrypted"] is True
    assert status["llm"]["api_key_masked"].endswith("9876")
    assert status["security"]["key_storage"]["llm_key_present"] is True


def test_source_hash_deterministic(tmp_path: Path) -> None:
    (tmp_path / "pocketmind").mkdir()
    (tmp_path / "pocketmind" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "main.js").write_text("console.log(1);\n", encoding="utf-8")
    first = compute_source_hash(tmp_path)
    second = compute_source_hash(tmp_path)
    assert first == second
    (tmp_path / "pocketmind" / "a.py").write_text("print('b')\n", encoding="utf-8")
    assert compute_source_hash(tmp_path) != first
