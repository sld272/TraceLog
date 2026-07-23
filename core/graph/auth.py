"""Microsoft Graph delegated authentication backed by the workspace.

Access and refresh tokens live only in MSAL's serialized cache.  This module
deliberately does not log authentication inputs or results.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import db, file_security

DEFAULT_GRAPH_CLIENT_ID = "a5811bbd-80ac-4bad-bafe-77ea8714b173"
CLIENT_ID_META_KEY = "graph.client_id"
TOKEN_CACHE_FILENAME = "graph_token_cache.json"
AUTHORITY = "https://login.microsoftonline.com/common"
INTERACTIVE_TIMEOUT_SECONDS = 300
# MSAL adds the reserved offline_access/openid/profile scopes itself.
GRAPH_SCOPES = ("Calendars.ReadWrite", "User.Read")


@dataclass
class _MsalRegistryEntry:
    client_id: str
    app: Any
    cache: Any
    token_cache_path: Path
    lock: threading.RLock = field(default_factory=threading.RLock)
    valid: bool = True


_MSAL_REGISTRY_LOCK = threading.RLock()
_MSAL_REGISTRY: dict[str, _MsalRegistryEntry] = {}
_MSAL_REGISTRY_CONTEXT: tuple[Path, Any, Any] | None = None
# Registry invalidation never waits for an entry lock.  A long-running login can
# therefore be cancelled/logout can complete; its detached cache is prevented
# from being persisted by the entry's ``valid`` flag.


class GraphAuthError(RuntimeError):
    """A safe-to-display Graph authentication failure."""


class GraphNotConfiguredError(GraphAuthError):
    """Raised when an authentication flow has no effective client id."""


class GraphAuth:
    def __init__(
        self,
        *,
        app_factory: Callable[..., Any] | None = None,
        cache_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._app_factory = app_factory
        self._cache_factory = cache_factory

    @property
    def token_cache_path(self) -> Path:
        return db.WORKSPACE_DIR / TOKEN_CACHE_FILENAME

    def client_id(self) -> str:
        return self.custom_client_id() or DEFAULT_GRAPH_CLIENT_ID

    def custom_client_id(self) -> str | None:
        row = db.query_one("SELECT value FROM meta WHERE key = ?", (CLIENT_ID_META_KEY,))
        if row is None:
            return None
        value = str(row["value"]).strip()
        return value or None

    def set_client_id(self, client_id: str) -> None:
        value = client_id.strip()
        if not value:
            raise ValueError("client_id 不能为空")
        with _MSAL_REGISTRY_LOCK:
            previous = self.client_id()
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (CLIENT_ID_META_KEY, value),
            )
            if previous != value:
                _invalidate_registry_locked()

    def clear_client_id(self) -> None:
        with _MSAL_REGISTRY_LOCK:
            previous = self.client_id()
            db.execute("DELETE FROM meta WHERE key = ?", (CLIENT_ID_META_KEY,))
            if previous != DEFAULT_GRAPH_CLIENT_ID:
                _invalidate_registry_locked()

    def client_id_info(self) -> dict[str, Any]:
        custom_value = self.custom_client_id()
        value = custom_value or DEFAULT_GRAPH_CLIENT_ID
        return {
            "configured": True,
            "using_default": custom_value is None,
            "client_id_tail": value[-4:],
        }

    def get_access_token(self) -> str | None:
        while True:
            with self._locked_entry(required=False) as entry:
                if entry is None:
                    return None
                accounts = entry.app.get_accounts()
                if not accounts:
                    if self._persist_if_changed(entry):
                        return None
                    continue
                result = entry.app.acquire_token_silent(
                    list(GRAPH_SCOPES), account=accounts[0]
                )
                if not self._persist_if_changed(entry):
                    continue
                if not isinstance(result, dict):
                    return None
                token = result.get("access_token")
                return str(token) if token else None

    def start_device_flow(self) -> dict[str, Any]:
        while True:
            with self._locked_entry(required=True) as entry:
                assert entry is not None
                flow = entry.app.initiate_device_flow(scopes=list(GRAPH_SCOPES))
                if not self._entry_is_current(entry):
                    continue
                if not isinstance(flow, dict) or not flow.get("user_code"):
                    raise GraphAuthError(
                        _safe_auth_error(flow, "无法启动 Microsoft 设备码登录")
                    )
                return flow

    def complete_device_flow(
        self,
        flow: dict[str, Any],
        *,
        exit_condition: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        with self._locked_entry(required=True) as entry:
            assert entry is not None
            kwargs = (
                {"exit_condition": exit_condition}
                if exit_condition is not None
                else {}
            )
            result = entry.app.acquire_token_by_device_flow(flow, **kwargs)
            if exit_condition is not None and exit_condition():
                raise GraphAuthError("Microsoft 登录已取消")
            if not self._persist_if_changed(entry):
                raise GraphAuthError("Microsoft 登录已取消")
            if not isinstance(result, dict) or not result.get("access_token"):
                raise GraphAuthError(_safe_auth_error(result, "Microsoft 登录未完成"))
            account = _account_info_from_app(entry.app)
            if not self._entry_is_current(entry):
                raise GraphAuthError("Microsoft 登录已取消")
            self._invalidate_registry()
            return account or {}

    def complete_interactive_flow(
        self,
        *,
        exit_condition: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        with self._locked_entry(required=True) as entry:
            assert entry is not None
            try:
                # MSAL generates PKCE parameters and hosts the callback on
                # http://localhost with a system-selected port.
                result = entry.app.acquire_token_interactive(
                    scopes=list(GRAPH_SCOPES),
                    timeout=INTERACTIVE_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                raise GraphAuthError(_safe_interactive_exception(exc)) from exc
            if exit_condition is not None and exit_condition():
                raise GraphAuthError("Microsoft 登录已取消")
            if not self._persist_if_changed(entry):
                raise GraphAuthError("Microsoft 登录已取消")
            if not isinstance(result, dict) or not result.get("access_token"):
                raise GraphAuthError(
                    _safe_auth_error(result, "Microsoft 浏览器登录未完成")
                )
            account = _account_info_from_app(entry.app)
            if not self._entry_is_current(entry):
                raise GraphAuthError("Microsoft 登录已取消")
            self._invalidate_registry()
            return account or {}

    def logout(self) -> None:
        with _MSAL_REGISTRY_LOCK:
            _invalidate_registry_locked()
            try:
                self.token_cache_path.unlink()
            except FileNotFoundError:
                pass

    def account_info(self) -> dict[str, Any] | None:
        while True:
            with self._locked_entry(required=False) as entry:
                if entry is None:
                    return None
                account = _account_info_from_app(entry.app)
                if self._entry_is_current(entry):
                    return account

    def _app(self, *, required: bool) -> Any | None:
        registered = self._registry_entry(required=required)
        return registered[1].app if registered is not None else None

    def _load_cache(self, cache: Any, path: Path) -> None:
        try:
            serialized = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise GraphAuthError("无法读取 Microsoft 登录缓存") from exc
        try:
            cache.deserialize(serialized)
            file_security.make_private(path)
        except (OSError, ValueError) as exc:
            raise GraphAuthError("Microsoft 登录缓存已损坏") from exc

    def _persist_if_changed(self, entry: _MsalRegistryEntry) -> bool:
        with _MSAL_REGISTRY_LOCK:
            if not self._entry_is_current_locked(entry):
                return False
            if not entry.cache.has_state_changed:
                return True
            path = entry.token_cache_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_name(
                f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                file_security.make_private(path.parent)
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(entry.cache.serialize())
                        handle.flush()
                        os.fsync(handle.fileno())
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                os.replace(temp_path, path)
                file_security.make_private(path)
            except OSError as exc:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                raise GraphAuthError("无法保存 Microsoft 登录缓存") from exc
            return True

    @contextmanager
    def _locked_entry(
        self, *, required: bool
    ) -> Iterator[_MsalRegistryEntry | None]:
        while True:
            registered = self._registry_entry(required=required)
            if registered is None:
                yield None
                return
            _, entry = registered
            entry.lock.acquire()
            if self._entry_is_current(entry):
                break
            entry.lock.release()
        try:
            yield entry
        finally:
            entry.lock.release()

    def _registry_entry(
        self, *, required: bool
    ) -> tuple[str, _MsalRegistryEntry] | None:
        global _MSAL_REGISTRY_CONTEXT

        with _MSAL_REGISTRY_LOCK:
            client_id = self.client_id()
            if client_id is None:
                if required:
                    raise GraphNotConfiguredError("请先配置 Microsoft 应用 client_id")
                return None
            self._ensure_msal()
            assert self._app_factory is not None
            assert self._cache_factory is not None
            path = self.token_cache_path
            context = (path, self._app_factory, self._cache_factory)
            if _MSAL_REGISTRY_CONTEXT != context:
                _invalidate_registry_locked()
                _MSAL_REGISTRY_CONTEXT = context
            entry = _MSAL_REGISTRY.get(client_id)
            if entry is None:
                cache = self._cache_factory()
                self._load_cache(cache, path)
                app = self._app_factory(
                    client_id=client_id,
                    authority=AUTHORITY,
                    token_cache=cache,
                )
                entry = _MsalRegistryEntry(
                    client_id=client_id,
                    app=app,
                    cache=cache,
                    token_cache_path=path,
                )
                _MSAL_REGISTRY[client_id] = entry
            return client_id, entry

    def _entry_is_current(self, entry: _MsalRegistryEntry) -> bool:
        with _MSAL_REGISTRY_LOCK:
            return self._entry_is_current_locked(entry)

    @staticmethod
    def _entry_is_current_locked(entry: _MsalRegistryEntry) -> bool:
        return entry.valid and _MSAL_REGISTRY.get(entry.client_id) is entry

    @staticmethod
    def _invalidate_registry() -> None:
        with _MSAL_REGISTRY_LOCK:
            _invalidate_registry_locked()

    def _ensure_msal(self) -> None:
        if self._cache_factory is not None and self._app_factory is not None:
            return
        try:
            import msal
        except ImportError as exc:
            raise GraphAuthError("Microsoft 日历认证依赖 msal 尚未安装") from exc
        self._cache_factory = msal.SerializableTokenCache
        self._app_factory = msal.PublicClientApplication


def _invalidate_registry_locked() -> None:
    for entry in _MSAL_REGISTRY.values():
        entry.valid = False
    _MSAL_REGISTRY.clear()


def _account_info_from_app(app: Any) -> dict[str, Any] | None:
    accounts = app.get_accounts()
    if not accounts:
        return None
    account = accounts[0]
    return {
        "username": account.get("username"),
        "name": account.get("name"),
        "home_account_id": account.get("home_account_id"),
    }


def _safe_auth_error(result: Any, fallback: str) -> str:
    if not isinstance(result, dict):
        return fallback
    code = result.get("error")
    description = result.get("error_description")
    if code and description:
        return f"{code}: {description}"
    if code:
        return str(code)
    return fallback


def _safe_interactive_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return f"无法完成 Microsoft 浏览器登录：{message}"
    return "无法完成 Microsoft 浏览器登录"
