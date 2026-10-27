"""API Key 本地加密存储（Windows）。

设计参考 PocketPet / PocketAgent 的 SecureCrypto + SecuredKeyStore：

- 算法：AES-256-GCM，随机 12 字节 IV，128 位认证标签；
- 格式：`PM1:` + Base64(IV + 密文 + tag)；
- 主密钥：32 字节随机密钥，用 **Windows DPAPI（当前用户）** 保护后存盘；
- 主密钥不出现在 settings.json；API Key 明文不落盘；
- 原子写；旧文件无法解密时先备份再处理，不静默覆盖。

安全边界：
  ✅ 防其他 Windows 用户直接读取（DPAPI 绑定用户）；
  ✅ 防普通复制文件到别的机器后解密；
  ❌ 防不住已以当前用户登录、能运行本次程序的人；
  ❌ 防不住调试/注入当前进程。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRETS_VERSION = "PM1:"
KEY_BYTES = 32
IV_BYTES = 12
_TAG_BITS = 128
_ENTROPY = b"PocketMind/secrets/v1"


class SecretStoreError(RuntimeError):
    """加密存储不可用/解密失败。不要把它当作空数据静默覆盖。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _dpapi_call(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("DPAPI 仅在 Windows 可用")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    fn.restype = wintypes.BOOL
    in_blob, _in_buf = _blob(data)
    entropy_blob, _entropy_buf = _blob(_ENTROPY)
    out_blob = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    ok = fn(
        ctypes.byref(in_blob),
        "PocketMind API secrets",
        ctypes.byref(entropy_blob),
        None,
        None,
        flags,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStoreError(f"DPAPI 调用失败（WinError {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(data, protect=True)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call(data, protect=False)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class SecretStore:
    """加密的 Key 表，文件在 `<data>/secrets/`。"""

    def __init__(self, data_dir: Path, *, _test_key: bytes | None = None):
        self.dir = Path(data_dir) / "secrets"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.master_file = self.dir / "master.key.dpapi"
        self.secrets_file = self.dir / "api_keys.enc"
        self.unreadable_file = self.dir / "api_keys.enc.unreadable-backup"
        self._test_key = _test_key
        self._lock = threading.RLock()
        self._cached_key: bytes | None = None
        self.last_error = ""

    # ------------------------------------------------------------------
    # 密钥
    # ------------------------------------------------------------------
    def _master_key(self) -> bytes:
        with self._lock:
            if self._cached_key:
                return self._cached_key
            if self._test_key is not None:
                if len(self._test_key) != KEY_BYTES:
                    raise SecretStoreError("测试密钥长度必须为 32 字节")
                self._cached_key = self._test_key
                return self._cached_key
            if self.master_file.exists():
                raw = self.master_file.read_bytes()
                try:
                    key = dpapi_unprotect(raw)
                except Exception as exc:
                    raise SecretStoreError(f"主密钥无法解密（DPAPI）: {exc}") from exc
                if len(key) != KEY_BYTES:
                    raise SecretStoreError("主密钥长度异常")
                self._cached_key = key
                return key
            key = os.urandom(KEY_BYTES)
            _atomic_write(self.master_file, dpapi_protect(key))
            self._cached_key = key
            return key

    # ------------------------------------------------------------------
    # AES-GCM
    # ------------------------------------------------------------------
    def _encrypt(self, plain: bytes) -> str:
        key = self._master_key()
        nonce = os.urandom(IV_BYTES)
        ct = AESGCM(key).encrypt(nonce, plain, _ENTROPY)
        return SECRETS_VERSION + base64.b64encode(nonce + ct).decode("ascii")

    def _decrypt(self, encoded: str) -> bytes:
        if not encoded.startswith(SECRETS_VERSION):
            raise SecretStoreError("未知的密钥文件格式")
        try:
            raw = base64.b64decode(encoded[len(SECRETS_VERSION):], validate=True)
        except Exception as exc:
            raise SecretStoreError("密钥文件 Base64 无效") from exc
        if len(raw) <= IV_BYTES:
            raise SecretStoreError("密钥文件长度不足")
        nonce, ct = raw[:IV_BYTES], raw[IV_BYTES:]
        try:
            return AESGCM(self._master_key()).decrypt(nonce, ct, _ENTROPY)
        except Exception as exc:
            raise SecretStoreError("API Key 表无法解密（密钥变化或文件被篡改）") from exc

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def load_all(self) -> dict[str, str]:
        with self._lock:
            if not self.secrets_file.exists():
                return {}
            try:
                encoded = self.secrets_file.read_text(encoding="utf-8").strip()
                if not encoded:
                    return {}
                raw = self._decrypt(encoded)
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise SecretStoreError("密钥表不是 JSON 对象")
                return {str(k): str(v) for k, v in data.items() if str(v)}
            except Exception as exc:
                self.last_error = str(exc)
                # 保留一份不可读原文，绝不静默覆盖
                if self.secrets_file.exists() and not self.unreadable_file.exists():
                    try:
                        _atomic_write(self.unreadable_file, self.secrets_file.read_bytes())
                    except Exception:
                        pass
                raise SecretStoreError(str(exc)) from exc

    def save_all(self, values: dict[str, str]) -> None:
        with self._lock:
            clean = {str(k): str(v) for k, v in values.items() if str(v).strip()}
            raw = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            encoded = self._encrypt(raw)
            _atomic_write(self.secrets_file, encoded.encode("utf-8"))

    def get(self, name: str) -> str:
        return self.load_all().get(name, "")

    def put(self, name: str, value: str) -> None:
        if not (value or "").strip():
            self.delete(name)
            return
        with self._lock:
            current = self.load_all()
            current[name] = value.strip()
            self.save_all(current)

    def delete(self, name: str) -> None:
        with self._lock:
            try:
                current = self.load_all()
            except SecretStoreError:
                current = {}
            current.pop(name, None)
            self.save_all(current)

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "backend": "AES-256-GCM + Windows DPAPI",
            "directory": str(self.dir),
            "master_key_present": self.master_file.exists(),
            "secrets_present": self.secrets_file.exists(),
            "error": "",
        }
        try:
            keys = self.load_all()
            info["key_names"] = sorted(keys)
            info["llm_key_present"] = bool(keys.get("llm"))
            info["embedding_key_present"] = bool(keys.get("embedding"))
        except Exception as exc:
            info["error"] = str(exc)
        return info
