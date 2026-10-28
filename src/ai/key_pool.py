"""Gemini API Key Pool with round-robin, cooldown, and secure storage (Section 6).

Features:
- Secure local storage (Windows DPAPI; fail closed if encryption is unavailable)
- Round-robin selection among healthy keys
- Per-key cooldown and failure backoff (429/quota -> cooldown, 401/403 -> invalid)
- Key masking (GET never returns full key; only **** + last 4 chars)
- On-demand health check (never consumes quota on page open)
"""

import base64
import ctypes
import json
import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings

logger = logging.getLogger(__name__)

# Windows DPAPI constants
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_encrypt(data: bytes) -> bytes:
    """Encrypt bytes using Windows DPAPI if available."""
    if sys.platform != "win32":
        return base64.b64encode(data)
    try:
        input_buffer = ctypes.create_string_buffer(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), "gemini_keys", None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
        ):
            enc = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return enc
    except Exception as e:
        raise RuntimeError(f"DPAPI encryption failed: {e}") from e
    raise RuntimeError("DPAPI encryption failed")


def _dpapi_decrypt(data: bytes) -> bytes:
    """Decrypt bytes using Windows DPAPI if available."""
    if sys.platform != "win32":
        return base64.b64decode(data)
    try:
        input_buffer = ctypes.create_string_buffer(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)
        ):
            dec = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return dec
    except Exception as e:
        raise RuntimeError(f"DPAPI decryption failed: {e}") from e
    raise RuntimeError("DPAPI decryption failed")


@dataclass
class KeyInfo:
    id: str
    key: str
    status: str = "healthy"  # healthy, cooldown, exhausted, invalid
    cooldown_until: float = 0.0
    failure_count: int = 0
    total_calls: int = 0
    last_latency_ms: Optional[float] = None
    last_error: Optional[str] = None
    model_cooldowns: Dict[str, float] = field(default_factory=dict)
    project_id: Optional[str] = None

    @property
    def masked(self) -> str:
        if len(self.key) <= 8:
            return "****"
        return f"****{self.key[-4:]}"

    def is_usable(self, model: Optional[str] = None) -> bool:
        if self.status == "invalid":
            return False
        if self.status == "cooldown" and time.time() < self.cooldown_until:
            return False
        if self.status in ("cooldown", "exhausted") and time.time() >= self.cooldown_until:
            self.status = "healthy"
            self.failure_count = 0
        return self.status == "healthy" and (model is None or time.time() >= self.model_cooldowns.get(model,0))


class GeminiKeyPool:
    """Thread-safe round-robin pool of Gemini API keys."""

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = Path(storage_dir or Path("config"))
        self.storage_file = self.storage_dir / "gemini_keys.bin"
        self._keys: List[KeyInfo] = []
        self._current_index: int = 0
        self._lock = threading.RLock()
        self._translation_next_call = {}
        self.load_keys()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._keys)

    def translation_keys(self, limit=5):
        """Snapshot worker identities without exposing credentials to the UI."""
        with self._lock:
            return [key for key in self._keys if key.status != 'invalid'][:limit]

    def wait_translation_turn(self, key_id, deadline, should_continue, model=None):
        """Space every actual request, including fallbacks, across translator jobs."""
        interval = 60.0 / max(1, min(12, settings.TRANSLATION_KEY_RPM))
        while True:
            if should_continue and not should_continue():
                from src.jobs.logger import TaskCancelled
                raise TaskCancelled()
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError('Translation deadline while waiting for key rate limit')
            with self._lock:
                key = next((k for k in self._keys if k.id == key_id), None)
                slots = [key_id]
                if model:
                    slots.append(('model', key.project_id if key and key.project_id else key_id, model))
                delay = max(self._translation_next_call.get(slot, 0) for slot in slots) - now
                if delay <= 0:
                    for slot in slots:
                        self._translation_next_call[slot] = now + interval
                    return
            time.sleep(min(.1, delay, deadline - now))

    def load_keys(self):
        """Load and decrypt keys from storage file."""
        self._keys.clear()
        if not self.storage_file.exists():
            # Check env var fallback
            env_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if env_key:
                self.add_keys([env_key])
            return

        try:
            encrypted = self.storage_file.read_bytes()
            decrypted = _dpapi_decrypt(encrypted)
            if decrypted:
                items = json.loads(decrypted.decode("utf-8"))
                for idx, item in enumerate(items):
                    k = item.get("key", "").strip()
                    if k:
                        self._keys.append(
                            KeyInfo(
                                id=f"key_{idx + 1}",
                                key=k,
                                status=item.get("status", "healthy"),
                                failure_count=item.get("failure_count", 0),
                                cooldown_until=float(item.get("cooldown_until", 0)),
                                model_cooldowns={m:float(until) for m,until in item.get('model_cooldowns',{}).items()},
                                project_id=item.get('project_id') or None,
                            )
                        )
        except Exception as e:
            logger.error(f"Failed to load keys from {self.storage_file}: {e}")

    def save_keys(self):
        """Encrypt and save keys to disk atomically."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        items = [{"key": k.key, "status": k.status, "failure_count": k.failure_count, "cooldown_until": k.cooldown_until,
                  'model_cooldowns':k.model_cooldowns,'project_id':k.project_id} for k in self._keys]
        data = json.dumps(items).encode("utf-8")
        enc = _dpapi_encrypt(data)

        tmp_file = self.storage_file.with_suffix(".tmp")
        tmp_file.write_bytes(enc)
        os.replace(str(tmp_file), str(self.storage_file))

    def add_keys(self, new_keys: List[str]) -> int:
        """Add new keys, strip whitespace and deduplicate."""
        with self._lock:
            existing = {k.key for k in self._keys}
            added = 0
            next_id = max((int(k.id.split("_")[-1]) for k in self._keys if k.id.startswith("key_") and k.id.split("_")[-1].isdigit()), default=0) + 1
            for raw in new_keys:
                cleaned = raw.strip()
                if cleaned and cleaned not in existing:
                    self._keys.append(KeyInfo(id=f"key_{next_id}", key=cleaned))
                    next_id += 1
                    existing.add(cleaned)
                    added += 1
            if added > 0:
                self.save_keys()
            return added

    def remove_key(self, key_id: str) -> bool:
        """Remove a key by ID."""
        with self._lock:
            orig_len = len(self._keys)
            self._keys = [k for k in self._keys if k.id != key_id]
            if len(self._keys) < orig_len:
                self.save_keys()
                return True
            return False

    def set_project(self, key_id, project_id):
        """The caller supplies the Cloud project; it cannot be inferred from a key."""
        with self._lock:
            key = next((k for k in self._keys if k.id == key_id), None)
            if key is None:
                return False
            key.project_id = project_id or None
            # Preserve the strictest known lane block when joining a quota group.
            peers = [k for k in self._keys if project_id and k.project_id == project_id]
            blocks = {}
            for peer in peers:
                for model, until in peer.model_cooldowns.items():
                    blocks[model] = max(blocks.get(model, 0), until)
            for peer in peers:
                peer.model_cooldowns.update(blocks)
            self.save_keys()
            return True

    def get_masked_list(self) -> List[Dict[str, Any]]:
        """Return masked key statuses for UI display (NEVER return raw keys)."""
        with self._lock:
            now = time.time()
            res = []
            for k in self._keys:
                if k.status in {"cooldown", "exhausted"} and now >= k.cooldown_until:
                    k.status = "healthy"
                res.append({
                    "id": k.id,
                    'project_id':k.project_id,
                    "masked_key": k.masked,
                    "status": k.status,
                    "total_calls": k.total_calls,
                    "latency_ms": k.last_latency_ms,
                    "last_error": k.last_error,
                    "cooldown_remaining_s": max(0, int(k.cooldown_until - now)) if k.status in {"cooldown", "exhausted"} else 0,
                    'model_cooldowns': {model:max(0,int(until-now)) for model,until in k.model_cooldowns.items() if until>now},
                })
            return res

    def get_next_key(self, model: Optional[str] = None, models: Optional[List[str]] = None) -> Optional[KeyInfo]:
        """Round-robin select next available healthy key."""
        with self._lock:
            if not self._keys:
                return None
            num_keys = len(self._keys)
            for _ in range(num_keys):
                idx = self._current_index % num_keys
                self._current_index += 1
                candidate = self._keys[idx]
                if (any(candidate.is_usable(name) for name in models) if models is not None else candidate.is_usable(model)):
                    return candidate
            return None

    def next_available_delay(self,models: List[str]) -> Optional[float]:
        with self._lock:
            now=time.time()
            ready=[max(k.cooldown_until if k.status in {'cooldown','exhausted'} else 0,
                       k.model_cooldowns.get(model,0))-now
                   for k in self._keys if k.status!='invalid' for model in models]
            return max(0,min(ready)) if ready else None

    def record_success(self, key_id: str, latency_ms: float, model: Optional[str] = None):
        """Record a successful API call for key."""
        with self._lock:
            for k in self._keys:
                if k.id == key_id:
                    k.total_calls += 1
                    k.failure_count = 0
                    k.last_latency_ms = round(latency_ms, 1)
                    if k.status!='invalid' and not (k.status in {'cooldown','exhausted'} and time.time()<k.cooldown_until):
                        k.status = "healthy"
                    if k.status=='healthy' and not any(until>time.time() for until in k.model_cooldowns.values()):
                        k.last_error = None
                    if model and time.time()>=k.model_cooldowns.get(model,0):
                        k.model_cooldowns.pop(model,None)
                    break
            self.save_keys()

    def record_failure(self, key_id: str, error_status: int, error_msg: str, retry_after: Optional[float] = None, model: Optional[str] = None):
        """Handle error and apply appropriate cooldown/invalid state.
        
        - 429: rate limit / quota -> cooldown (60s * failure_count, max 600s)
        - 401/403: auth error / invalid key -> invalid
        - 5xx / timeout: temporary failure -> short cooldown (15s)
        """
        with self._lock:
            for k in self._keys:
                if k.id == key_id:
                    k.failure_count += 1
                    k.last_error = f"HTTP {error_status}: {error_msg[:120]}"

                    if error_status in (401, 403):
                        k.status = "invalid"
                        logger.warning(f"Key {k.masked} marked INVALID due to HTTP {error_status}")
                    elif error_status == 429:
                        base = min(600, 60 * (2 ** max(0, k.failure_count - 1)))
                        cooldown = min(600, base + random.uniform(0, min(15, base * 0.1)))
                        if retry_after is not None:
                            cooldown = max(cooldown, retry_after)
                        if model:
                            k.model_cooldowns[model] = time.time()+cooldown
                            if k.project_id:
                                for peer in self._keys:
                                    if peer.project_id == k.project_id:
                                        peer.model_cooldowns[model] = max(peer.model_cooldowns.get(model, 0), k.model_cooldowns[model])
                        else:
                            k.status = "exhausted" if cooldown >= 3600 else "cooldown"
                            k.cooldown_until = time.time() + cooldown
                        logger.warning(f"Key {k.masked} cooldown for {cooldown:.1f}s due to quota (429)")
                    elif model:
                        until = time.time() + (3600 if error_status == 404 else 15)
                        for peer in self._keys:
                            if peer is k or (k.project_id and peer.project_id == k.project_id):
                                peer.model_cooldowns[model] = max(peer.model_cooldowns.get(model, 0), until)
                    else:
                        k.status = "cooldown"
                        k.cooldown_until = time.time() + 15
                    break
            self.save_keys()

    def test_key(self, key_id: str) -> Tuple[bool, str, float]:
        """Test a key on demand without automatic trigger on UI open."""
        target = None
        for k in self._keys:
            if k.id == key_id:
                target = k
                break
        if not target:
            return False, "Không tìm thấy key", 0.0

        # Reuse bounded model-aware calls, but never rotate to another key
        # while the user is explicitly testing this particular credential.
        from types import SimpleNamespace
        from src.translation.batch_translator import BatchTranslator
        adapter = SimpleNamespace(count=1,
            get_next_key=lambda **kwargs:target if target.is_usable() else None,
            record_success=self.record_success,record_failure=self.record_failure)
        started=time.monotonic()
        try:
            result=BatchTranslator(pool=adapter)._call_gemini_with_rotation(
                'Return an empty JSON array [] to confirm API connectivity.')
            elapsed=(time.monotonic()-started)*1000
            return True,'Key active ('+getattr(result,'model',settings.GEMINI_MODEL)+')',elapsed
        except Exception as exc:
            from src.jobs.logger import mask_secrets
            message=mask_secrets(str(exc)).replace(target.key,'***REDACTED***')
            return False,message[:150],(time.monotonic()-started)*1000


# Global key pool singleton
key_pool = GeminiKeyPool()

