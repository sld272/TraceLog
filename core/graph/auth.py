"""Microsoft Graph delegated authentication backed by the workspace.

Access and refresh tokens live only in MSAL's serialized cache.  This module
deliberately does not log authentication inputs or results.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import db

DEFAULT_GRAPH_CLIENT_ID = "a5811bbd-80ac-4bad-bafe-77ea8714b173"
CLIENT_ID_META_KEY = "graph.client_id"
TOKEN_CACHE_FILENAME = "graph_token_cache.json"
AUTHORITY = "https://login.microsoftonline.com/common"
INTERACTIVE_TIMEOUT_SECONDS = 300
# MSAL adds the reserved offline_access/openid/profile scopes itself.
GRAPH_SCOPES = ("Calendars.ReadWrite", "User.Read")


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
        self._cache: Any | None = None
        if cache_factory is not None:
            self._cache = cache_factory()
            self._load_cache()

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
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (CLIENT_ID_META_KEY, value),
        )

    def clear_client_id(self) -> None:
        db.execute("DELETE FROM meta WHERE key = ?", (CLIENT_ID_META_KEY,))

    def client_id_info(self) -> dict[str, Any]:
        custom_value = self.custom_client_id()
        value = custom_value or DEFAULT_GRAPH_CLIENT_ID
        return {
            "configured": True,
            "using_default": custom_value is None,
            "client_id_tail": value[-4:],
        }

    def get_access_token(self) -> str | None:
        app = self._app(required=False)
        if app is None:
            return None
        accounts = app.get_accounts()
        if not accounts:
            self._persist_if_changed()
            return None
        result = app.acquire_token_silent(list(GRAPH_SCOPES), account=accounts[0])
        self._persist_if_changed()
        if not isinstance(result, dict):
            return None
        token = result.get("access_token")
        return str(token) if token else None

    def start_device_flow(self) -> dict[str, Any]:
        app = self._app(required=True)
        flow = app.initiate_device_flow(scopes=list(GRAPH_SCOPES))
        if not isinstance(flow, dict) or not flow.get("user_code"):
            raise GraphAuthError(_safe_auth_error(flow, "无法启动 Microsoft 设备码登录"))
        return flow

    def complete_device_flow(
        self,
        flow: dict[str, Any],
        *,
        exit_condition: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        app = self._app(required=True)
        kwargs = {"exit_condition": exit_condition} if exit_condition is not None else {}
        result = app.acquire_token_by_device_flow(flow, **kwargs)
        if exit_condition is not None and exit_condition():
            raise GraphAuthError("Microsoft 登录已取消")
        self._persist_if_changed()
        if not isinstance(result, dict) or not result.get("access_token"):
            raise GraphAuthError(_safe_auth_error(result, "Microsoft 登录未完成"))
        account = self.account_info()
        return account or {}

    def complete_interactive_flow(
        self,
        *,
        exit_condition: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        app = self._app(required=True)
        try:
            # MSAL generates PKCE parameters and hosts the callback on
            # http://localhost with a system-selected port.
            result = app.acquire_token_interactive(
                scopes=list(GRAPH_SCOPES),
                timeout=INTERACTIVE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise GraphAuthError(_safe_interactive_exception(exc)) from exc
        if exit_condition is not None and exit_condition():
            raise GraphAuthError("Microsoft 登录已取消")
        self._persist_if_changed()
        if not isinstance(result, dict) or not result.get("access_token"):
            raise GraphAuthError(_safe_auth_error(result, "Microsoft 浏览器登录未完成"))
        account = self.account_info()
        return account or {}

    def logout(self) -> None:
        path = self.token_cache_path
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self._cache = self._cache_factory() if self._cache_factory is not None else None

    def account_info(self) -> dict[str, Any] | None:
        app = self._app(required=False)
        if app is None:
            return None
        accounts = app.get_accounts()
        if not accounts:
            return None
        account = accounts[0]
        return {
            "username": account.get("username"),
            "name": account.get("name"),
            "home_account_id": account.get("home_account_id"),
        }

    def _app(self, *, required: bool) -> Any | None:
        client_id = self.client_id()
        if client_id is None:
            if required:
                raise GraphNotConfiguredError("请先配置 Microsoft 应用 client_id")
            return None
        self._ensure_msal()
        assert self._app_factory is not None
        assert self._cache is not None
        return self._app_factory(
            client_id=client_id,
            authority=AUTHORITY,
            token_cache=self._cache,
        )

    def _load_cache(self) -> None:
        assert self._cache is not None
        path = self.token_cache_path
        try:
            serialized = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise GraphAuthError("无法读取 Microsoft 登录缓存") from exc
        try:
            self._cache.deserialize(serialized)
            os.chmod(path, 0o600)
        except (OSError, ValueError) as exc:
            raise GraphAuthError("Microsoft 登录缓存已损坏") from exc

    def _persist_if_changed(self) -> None:
        assert self._cache is not None
        if not self._cache.has_state_changed:
            return
        path = self.token_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(self._cache.serialize())
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temp_path, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise GraphAuthError("无法保存 Microsoft 登录缓存") from exc

    def _ensure_msal(self) -> None:
        if self._cache is not None and self._app_factory is not None:
            return
        try:
            import msal
        except ImportError as exc:
            raise GraphAuthError("Microsoft 日历认证依赖 msal 尚未安装") from exc
        self._cache_factory = msal.SerializableTokenCache
        self._cache = msal.SerializableTokenCache()
        self._app_factory = msal.PublicClientApplication
        self._load_cache()


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
