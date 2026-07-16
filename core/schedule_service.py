"""Multi-account calendar storage and Outlook synchronization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta, timezone
import threading
from typing import Any
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import db, goal_schedule_service
from core.graph.auth import GraphAuth, GraphAuthError
from core.graph.client import GraphClient, GraphHTTPError

LOCAL_TIMEZONE_NAME = "Asia/Shanghai"
LOCAL_TIMEZONE = ZoneInfo(LOCAL_TIMEZONE_NAME)
OUTLOOK_ACCOUNT_ID = "outlook"
LOCAL_ACCOUNT_ID = "local"
OUTLOOK_DISPLAY_NAME = "Outlook"
LOCAL_DISPLAY_NAME = "本地日历"
DELTA_LINK_META_KEY = "graph.delta_link"
LAST_SYNC_AT_META_KEY = "graph.last_sync_at"
WINDOW_START_META_KEY = "graph.window_start"
WINDOW_END_META_KEY = "graph.window_end"
_SYNC_LOCK = threading.Lock()


class ScheduleNotConnectedError(RuntimeError):
    """Raised only by write operations that require an Outlook connection."""


class NoWritableAccountError(ScheduleNotConnectedError):
    """Raised when neither Outlook nor a local calendar can accept an event."""


class ScheduleService:
    def __init__(
        self,
        *,
        auth: Any | None = None,
        graph_factory: Callable[[Callable[[], str | None]], Any] = GraphClient,
        today: Callable[[], date] | None = None,
        clock: Callable[[], float] = db.now_ts,
    ) -> None:
        self.auth = auth or GraphAuth()
        self._graph_factory = graph_factory
        self._today = today or (lambda: datetime.now(LOCAL_TIMEZONE).date())
        self._clock = clock

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = db.query_all(
            """
            SELECT account.id, account.provider, account.display_name,
                   COUNT(event.id) AS event_count
            FROM calendar_accounts AS account
            LEFT JOIN schedule_events AS event ON event.account_id = account.id
            GROUP BY account.id, account.provider, account.display_name, account.created_at
            ORDER BY account.created_at, account.id
            """
        )
        return [
            {
                "id": str(row["id"]),
                "provider": str(row["provider"]),
                "display_name": str(row["display_name"]),
                "event_count": int(row["event_count"]),
            }
            for row in rows
        ]

    def create_local_account(self) -> dict[str, Any]:
        with db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO calendar_accounts(
                    id, provider, display_name, created_at
                ) VALUES (?, 'local', ?, ?)
                """,
                (LOCAL_ACCOUNT_ID, LOCAL_DISPLAY_NAME, self._clock()),
            )
            if cursor.rowcount == 0:
                raise ValueError("本地日历已存在")
        account = next(
            item for item in self.list_accounts() if item["id"] == LOCAL_ACCOUNT_ID
        )
        return account

    def delete_local_account(self, *, delete_events: bool) -> int:
        if delete_events is not True:
            raise ValueError("必须明确确认删除本地日历及其全部日程")
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS event_count FROM schedule_events WHERE account_id = ?",
                (LOCAL_ACCOUNT_ID,),
            ).fetchone()
            deleted_events = int(row["event_count"]) if row is not None else 0
            conn.execute(
                """
                DELETE FROM goal_schedule_links
                WHERE event_id IN (
                    SELECT id FROM schedule_events WHERE account_id = ?
                )
                """,
                (LOCAL_ACCOUNT_ID,),
            )
            conn.execute(
                "DELETE FROM schedule_events WHERE account_id = ?",
                (LOCAL_ACCOUNT_ID,),
            )
            conn.execute(
                "DELETE FROM calendar_accounts WHERE id = ? AND provider = 'local'",
                (LOCAL_ACCOUNT_ID,),
            )
        return deleted_events

    def status(self) -> dict[str, Any]:
        configured = self.auth.client_id() is not None
        token = self._access_token() if configured else None
        connected = token is not None
        account = self._account_info() if connected else None
        if connected:
            self._ensure_outlook_account(account)
        window_start, window_end = self._window_dates()
        last_sync = _meta_value(LAST_SYNC_AT_META_KEY)
        return {
            "configured": configured,
            "connected": connected,
            "account": account,
            "last_sync_at": float(last_sync) if last_sync is not None else None,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "accounts": self.list_accounts(),
        }

    def list_events(self, start_date: date, end_date: date) -> dict[str, Any]:
        _validate_date_range(start_date, end_date)
        configured = self.auth.client_id() is not None
        token = self._access_token() if configured else None
        connected = token is not None
        if connected:
            self._ensure_outlook_account(self._account_info())
        accounts = self.list_accounts()
        has_local_account = any(
            account["id"] == LOCAL_ACCOUNT_ID for account in accounts
        )
        if not connected and not has_local_account:
            return {
                "configured": configured,
                "connected": False,
                "status": "not_connected",
                "events": [],
                "accounts": accounts,
            }
        start_ts, end_ts = _date_range_epoch(start_date, end_date)
        rows = db.query_all(
            """
            SELECT event.*, account.provider
            FROM schedule_events AS event
            LEFT JOIN calendar_accounts AS account ON account.id = event.account_id
            WHERE event.end_ts > ? AND event.start_ts < ?
              AND event.is_cancelled = 0
            ORDER BY event.start_ts, event.end_ts, event.id
            """,
            (start_ts, end_ts),
        )
        events = [_row_to_event(row) for row in rows]
        _attach_goal_links(events)
        return {
            "configured": configured,
            "connected": connected,
            "status": "ok",
            "events": events,
            "accounts": self.list_accounts(),
        }

    def sync(self, *, force: bool = False) -> dict[str, Any]:
        with _SYNC_LOCK:
            return self._sync(force=force)

    def _sync(self, *, force: bool) -> dict[str, Any]:
        configured = self.auth.client_id() is not None
        token = self._access_token() if configured else None
        if token is None:
            last_sync = _meta_value(LAST_SYNC_AT_META_KEY)
            return {
                "ok": False,
                "configured": configured,
                "connected": False,
                "status": "not_connected",
                "upserted": 0,
                "deleted": 0,
                "last_sync_at": float(last_sync) if last_sync is not None else None,
            }

        self._ensure_outlook_account(self._account_info())
        graph = self._graph_factory(lambda: token)
        window_start, window_end = self._window_dates()
        stored_window = (
            _meta_value(WINDOW_START_META_KEY),
            _meta_value(WINDOW_END_META_KEY),
        )
        delta_link = None if force else _meta_value(DELTA_LINK_META_KEY)
        if stored_window != (window_start.isoformat(), window_end.isoformat()):
            delta_link = None

        full_refresh = delta_link is None
        try:
            result = self._fetch_delta(graph, window_start, window_end, delta_link)
        except GraphHTTPError as exc:
            if delta_link is None or exc.status_code != 410:
                raise
            full_refresh = True
            result = self._fetch_delta(graph, window_start, window_end, None)

        now = self._clock()
        upserted = 0
        deleted = 0
        with db.transaction() as conn:
            previous_outlook_ids: list[str] = []
            if full_refresh:
                previous_outlook_ids = [
                    str(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM schedule_events WHERE account_id = ?",
                        (OUTLOOK_ACCOUNT_ID,),
                    ).fetchall()
                ]
                conn.execute(
                    "DELETE FROM schedule_events WHERE account_id = ?",
                    (OUTLOOK_ACCOUNT_ID,),
                )
            for raw_event in result["events"]:
                event_id = str(raw_event.get("id") or "")
                if not event_id:
                    raise ValueError("Graph 日程缺少 id")
                if raw_event.get("@removed") is not None:
                    conn.execute(
                        """
                        DELETE FROM goal_schedule_links
                        WHERE event_id = ?
                          AND EXISTS (
                              SELECT 1 FROM schedule_events
                              WHERE id = ? AND account_id = ?
                          )
                        """,
                        (event_id, event_id, OUTLOOK_ACCOUNT_ID),
                    )
                    cursor = conn.execute(
                        """
                        DELETE FROM schedule_events
                        WHERE id = ? AND account_id = ?
                        """,
                        (event_id, OUTLOOK_ACCOUNT_ID),
                    )
                    deleted += max(0, cursor.rowcount)
                    continue
                normalized = _normalize_graph_event(raw_event, synced_at=now)
                _upsert_event(conn, normalized)
                if normalized["is_cancelled"]:
                    conn.execute(
                        """
                        DELETE FROM goal_schedule_links
                        WHERE event_id = ?
                          AND EXISTS (
                              SELECT 1 FROM schedule_events
                              WHERE id = ? AND account_id = ?
                          )
                        """,
                        (event_id, event_id, OUTLOOK_ACCOUNT_ID),
                    )
                upserted += 1
            if full_refresh:
                for previous_id in previous_outlook_ids:
                    if conn.execute(
                        """
                        SELECT 1 FROM schedule_events
                        WHERE id = ? AND account_id = ?
                        """,
                        (previous_id, OUTLOOK_ACCOUNT_ID),
                    ).fetchone() is None:
                        conn.execute(
                            "DELETE FROM goal_schedule_links WHERE event_id = ?",
                            (previous_id,),
                        )
            _set_meta(conn, DELTA_LINK_META_KEY, str(result["delta_link"]))
            _set_meta(conn, LAST_SYNC_AT_META_KEY, str(now))
            _set_meta(conn, WINDOW_START_META_KEY, window_start.isoformat())
            _set_meta(conn, WINDOW_END_META_KEY, window_end.isoformat())
        return {
            "ok": True,
            "configured": True,
            "connected": True,
            "status": "ok",
            "upserted": upserted,
            "deleted": deleted,
            "last_sync_at": now,
        }

    def create_event(
        self,
        *,
        subject: str,
        event_date: date,
        start_time: time | None = None,
        end_time: time | None = None,
        all_day: bool = False,
        goal_id: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        if goal_id is not None and db.query_one("SELECT 1 FROM goals WHERE id = ?", (goal_id,)) is None:
            raise goal_schedule_service.GoalNotFoundError("goal not found")
        payload = _create_payload(
            subject=subject,
            event_date=event_date,
            start_time=start_time,
            end_time=end_time,
            all_day=all_day,
        )
        target, graph = self._writable_target(account_id)
        if target == LOCAL_ACCOUNT_ID:
            normalized = _normalize_local_event(
                event_id=f"local_{uuid.uuid4().hex}",
                payload=payload,
                synced_at=self._clock(),
            )
        else:
            assert graph is not None
            raw_event = graph.create_event(payload)
            normalized = _normalize_graph_event(raw_event, synced_at=self._clock())
        with db.transaction() as conn:
            _upsert_event(conn, normalized)
            if goal_id is not None:
                goal_schedule_service.link(goal_id, str(normalized["id"]), conn=conn)
        created = _event_dict(normalized)
        _attach_goal_links([created])
        return created

    def update_event(self, event_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        row = db.query_one("SELECT * FROM schedule_events WHERE id = ?", (event_id,))
        if row is None:
            raise ValueError("本地缓存中找不到该日程，请先同步")
        payload = self._update_payload(event_id, changes)
        if not payload:
            raise ValueError("没有可更新的日程字段")
        account_id = str(row["account_id"] or OUTLOOK_ACCOUNT_ID)
        if account_id == LOCAL_ACCOUNT_ID:
            normalized = _apply_local_update(row, payload, synced_at=self._clock())
        elif account_id == OUTLOOK_ACCOUNT_ID:
            graph = self._connected_graph()
            raw_event = graph.update_event(event_id, payload)
            normalized = _normalize_graph_event(raw_event, synced_at=self._clock())
        else:
            raise ValueError("该日历账号暂不支持写入")
        with db.transaction() as conn:
            _upsert_event(conn, normalized)
        updated = _event_dict(normalized)
        _attach_goal_links([updated])
        return updated

    def delete_event(self, event_id: str) -> None:
        row = db.query_one(
            "SELECT account_id FROM schedule_events WHERE id = ?", (event_id,)
        )
        if row is None:
            raise ValueError("本地缓存中找不到该日程，请先同步")
        account_id = str(row["account_id"] or OUTLOOK_ACCOUNT_ID)
        if account_id == OUTLOOK_ACCOUNT_ID:
            graph = self._connected_graph()
            graph.delete_event(event_id)
        elif account_id != LOCAL_ACCOUNT_ID:
            raise ValueError("该日历账号暂不支持写入")
        with db.transaction() as conn:
            conn.execute("DELETE FROM goal_schedule_links WHERE event_id = ?", (event_id,))
            conn.execute(
                "DELETE FROM schedule_events WHERE id = ? AND account_id = ?",
                (event_id, account_id),
            )

    def logout(self) -> None:
        with _SYNC_LOCK:
            self.auth.logout()
            with db.transaction() as conn:
                conn.execute(
                    """
                    DELETE FROM goal_schedule_links
                    WHERE event_id IN (
                        SELECT id FROM schedule_events WHERE account_id = ?
                    )
                    """,
                    (OUTLOOK_ACCOUNT_ID,),
                )
                conn.execute(
                    "DELETE FROM schedule_events WHERE account_id = ?",
                    (OUTLOOK_ACCOUNT_ID,),
                )
                conn.execute(
                    "DELETE FROM meta WHERE key IN (?, ?, ?, ?)",
                    (
                        DELTA_LINK_META_KEY,
                        LAST_SYNC_AT_META_KEY,
                        WINDOW_START_META_KEY,
                        WINDOW_END_META_KEY,
                    ),
                )

    def _connected_graph(self) -> Any:
        configured = self.auth.client_id() is not None
        token = self._access_token() if configured else None
        if token is None:
            raise ScheduleNotConnectedError("Microsoft 日历尚未连接")
        self._ensure_outlook_account(self._account_info())
        return self._graph_factory(lambda: token)

    def _writable_target(self, requested_account_id: str | None) -> tuple[str, Any | None]:
        if requested_account_id is not None:
            row = db.query_one(
                "SELECT id, provider FROM calendar_accounts WHERE id = ?",
                (requested_account_id,),
            )
            if row is None:
                if requested_account_id == OUTLOOK_ACCOUNT_ID:
                    return OUTLOOK_ACCOUNT_ID, self._connected_graph()
                raise ValueError("日历账号不存在")
            provider = str(row["provider"])
            if provider == "local":
                return str(row["id"]), None
            if provider == "outlook":
                return str(row["id"]), self._connected_graph()
            raise ValueError("该日历账号暂不支持写入")

        configured = self.auth.client_id() is not None
        token = self._access_token() if configured else None
        if token is not None:
            self._ensure_outlook_account(self._account_info())
            return OUTLOOK_ACCOUNT_ID, self._graph_factory(lambda: token)
        if db.query_one(
            "SELECT 1 FROM calendar_accounts WHERE id = ? AND provider = 'local'",
            (LOCAL_ACCOUNT_ID,),
        ) is not None:
            return LOCAL_ACCOUNT_ID, None
        raise NoWritableAccountError("没有可写日历账号")

    def _access_token(self) -> str | None:
        try:
            return self.auth.get_access_token()
        except GraphAuthError:
            return None

    def _account_info(self) -> dict[str, Any] | None:
        account_info = getattr(self.auth, "account_info", None)
        if account_info is None:
            return None
        try:
            value = account_info()
        except GraphAuthError:
            return None
        return dict(value) if isinstance(value, Mapping) else None

    def _ensure_outlook_account(self, account: Mapping[str, Any] | None) -> None:
        username = str((account or {}).get("username") or "").strip()
        display_name = username or OUTLOOK_DISPLAY_NAME
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO calendar_accounts(
                    id, provider, display_name, created_at
                )
                VALUES (?, 'outlook', ?, ?)
                """,
                (OUTLOOK_ACCOUNT_ID, display_name, db.now_ts()),
            )
            if username:
                conn.execute(
                    """
                    UPDATE calendar_accounts
                    SET provider = 'outlook', display_name = ?
                    WHERE id = ?
                    """,
                    (username, OUTLOOK_ACCOUNT_ID),
                )

    def _window_dates(self) -> tuple[date, date]:
        today = self._today()
        return today - timedelta(days=60), today + timedelta(days=365)

    def _fetch_delta(
        self,
        graph: Any,
        window_start: date,
        window_end: date,
        delta_link: str | None,
    ) -> dict[str, Any]:
        if delta_link:
            return graph.calendarview_delta(delta_link=delta_link)
        start_dt = datetime.combine(window_start, time.min, LOCAL_TIMEZONE)
        exclusive_end = datetime.combine(window_end + timedelta(days=1), time.min, LOCAL_TIMEZONE)
        return graph.calendarview_delta(
            start=start_dt.isoformat(timespec="seconds"),
            end=exclusive_end.isoformat(timespec="seconds"),
        )

    def _update_payload(self, event_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"subject", "date", "start_time", "end_time", "all_day"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"不支持更新字段：{', '.join(sorted(unknown))}")
        payload: dict[str, Any] = {}
        if "subject" in changes:
            if changes["subject"] is None:
                raise ValueError("subject 不能为空")
            subject = str(changes["subject"]).strip()
            if not subject:
                raise ValueError("subject 不能为空")
            payload["subject"] = subject
        time_keys = {"date", "start_time", "end_time", "all_day"}
        if time_keys.intersection(changes):
            null_fields = [key for key in time_keys.intersection(changes) if changes[key] is None]
            if null_fields:
                raise ValueError(f"字段不能为 null：{', '.join(sorted(null_fields))}")
            row = db.query_one("SELECT * FROM schedule_events WHERE id = ?", (event_id,))
            if row is None:
                raise ValueError("本地缓存中找不到该日程，请先同步")
            existing_start = datetime.fromisoformat(str(row["start_local"]))
            existing_end = datetime.fromisoformat(str(row["end_local"]))
            event_date = changes.get("date", existing_start.date())
            if not isinstance(event_date, date):
                raise ValueError("date 格式无效")
            all_day = bool(changes.get("all_day", bool(row["all_day"])))
            start_value = changes.get("start_time", existing_start.time().replace(microsecond=0))
            end_value = changes.get("end_time", existing_end.time().replace(microsecond=0))
            time_payload = _create_payload(
                subject=str(row["subject"]),
                event_date=event_date,
                start_time=start_value if isinstance(start_value, time) else None,
                end_time=end_value if isinstance(end_value, time) else None,
                all_day=all_day,
            )
            payload.update(
                {
                    "start": time_payload["start"],
                    "end": time_payload["end"],
                    "isAllDay": time_payload["isAllDay"],
                }
            )
        return payload


def _validate_date_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ValueError("end 不能早于 start")


def _date_range_epoch(start_date: date, end_date: date) -> tuple[float, float]:
    start_dt = datetime.combine(start_date, time.min, LOCAL_TIMEZONE)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, LOCAL_TIMEZONE)
    return start_dt.timestamp(), end_dt.timestamp()


def _create_payload(
    *,
    subject: str,
    event_date: date,
    start_time: time | None,
    end_time: time | None,
    all_day: bool,
) -> dict[str, Any]:
    clean_subject = subject.strip()
    if not clean_subject:
        raise ValueError("subject 不能为空")
    if all_day:
        start_dt = datetime.combine(event_date, time.min)
        end_dt = start_dt + timedelta(days=1)
    else:
        start_value = start_time or time(hour=9)
        start_dt = datetime.combine(event_date, start_value)
        end_dt = datetime.combine(event_date, end_time) if end_time else start_dt + timedelta(hours=1)
        if end_dt <= start_dt:
            raise ValueError("end_time 必须晚于 start_time")
    return {
        "subject": clean_subject,
        "start": {
            "dateTime": start_dt.isoformat(timespec="seconds"),
            "timeZone": LOCAL_TIMEZONE_NAME,
        },
        "end": {
            "dateTime": end_dt.isoformat(timespec="seconds"),
            "timeZone": LOCAL_TIMEZONE_NAME,
        },
        "isAllDay": all_day,
    }


def _normalize_graph_event(raw: Mapping[str, Any], *, synced_at: float) -> dict[str, Any]:
    event_id = str(raw.get("id") or "")
    if not event_id:
        raise ValueError("Graph 日程缺少 id")
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise ValueError("Graph 日程缺少 start/end")
    start_dt = _parse_graph_datetime(start)
    end_dt = _parse_graph_datetime(end)
    if end_dt <= start_dt:
        raise ValueError("Graph 日程结束时间无效")
    location = raw.get("location")
    location_name = location.get("displayName") if isinstance(location, Mapping) else None
    return {
        "id": event_id,
        "account_id": OUTLOOK_ACCOUNT_ID,
        "provider": "outlook",
        "subject": str(raw.get("subject") or ""),
        "body_preview": raw.get("bodyPreview"),
        "start_ts": start_dt.astimezone(timezone.utc).timestamp(),
        "end_ts": end_dt.astimezone(timezone.utc).timestamp(),
        "start_local": start_dt.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None).isoformat(timespec="seconds"),
        "end_local": end_dt.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None).isoformat(timespec="seconds"),
        "all_day": 1 if raw.get("isAllDay") else 0,
        "location": str(location_name) if location_name else None,
        "web_link": raw.get("webLink"),
        "series_master_id": raw.get("seriesMasterId"),
        "is_cancelled": 1 if raw.get("isCancelled") else 0,
        "change_key": raw.get("changeKey"),
        "synced_at": synced_at,
    }


def _normalize_local_event(
    *,
    event_id: str,
    payload: Mapping[str, Any],
    synced_at: float,
) -> dict[str, Any]:
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise ValueError("本地日程缺少 start/end")
    start_dt = _parse_graph_datetime(start)
    end_dt = _parse_graph_datetime(end)
    return {
        "id": event_id,
        "account_id": LOCAL_ACCOUNT_ID,
        "provider": "local",
        "subject": str(payload.get("subject") or ""),
        "body_preview": None,
        "start_ts": start_dt.astimezone(timezone.utc).timestamp(),
        "end_ts": end_dt.astimezone(timezone.utc).timestamp(),
        "start_local": start_dt.astimezone(LOCAL_TIMEZONE)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds"),
        "end_local": end_dt.astimezone(LOCAL_TIMEZONE)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds"),
        "all_day": 1 if payload.get("isAllDay") else 0,
        "location": None,
        "web_link": None,
        "series_master_id": None,
        "is_cancelled": 0,
        "change_key": None,
        "synced_at": synced_at,
    }


def _apply_local_update(
    row: Any,
    payload: Mapping[str, Any],
    *,
    synced_at: float,
) -> dict[str, Any]:
    updated = dict(row)
    updated["account_id"] = LOCAL_ACCOUNT_ID
    updated["provider"] = "local"
    if "subject" in payload:
        updated["subject"] = payload["subject"]
    start = payload.get("start")
    end = payload.get("end")
    if isinstance(start, Mapping) and isinstance(end, Mapping):
        start_dt = _parse_graph_datetime(start)
        end_dt = _parse_graph_datetime(end)
        updated.update(
            {
                "start_ts": start_dt.astimezone(timezone.utc).timestamp(),
                "end_ts": end_dt.astimezone(timezone.utc).timestamp(),
                "start_local": start_dt.astimezone(LOCAL_TIMEZONE)
                .replace(tzinfo=None)
                .isoformat(timespec="seconds"),
                "end_local": end_dt.astimezone(LOCAL_TIMEZONE)
                .replace(tzinfo=None)
                .isoformat(timespec="seconds"),
                "all_day": 1 if payload.get("isAllDay") else 0,
            }
        )
    updated["synced_at"] = synced_at
    return updated


def _parse_graph_datetime(value: Mapping[str, Any]) -> datetime:
    raw = str(value.get("dateTime") or "").strip()
    if not raw:
        raise ValueError("Graph 日程时间为空")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("Graph 日程时间格式无效") from exc
    if parsed.tzinfo is None:
        zone_name = str(value.get("timeZone") or LOCAL_TIMEZONE_NAME)
        aliases = {
            "China Standard Time": LOCAL_TIMEZONE_NAME,
            "UTC": "UTC",
        }
        try:
            zone = ZoneInfo(aliases.get(zone_name, zone_name))
        except ZoneInfoNotFoundError:
            zone = LOCAL_TIMEZONE
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _upsert_event(conn: Any, event: Mapping[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO schedule_events(
            id, account_id, subject, body_preview, start_ts, end_ts, start_local, end_local,
            all_day, location, web_link, series_master_id, is_cancelled,
            change_key, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            account_id = excluded.account_id,
            subject = excluded.subject,
            body_preview = excluded.body_preview,
            start_ts = excluded.start_ts,
            end_ts = excluded.end_ts,
            start_local = excluded.start_local,
            end_local = excluded.end_local,
            all_day = excluded.all_day,
            location = excluded.location,
            web_link = excluded.web_link,
            series_master_id = excluded.series_master_id,
            is_cancelled = excluded.is_cancelled,
            change_key = excluded.change_key,
            synced_at = excluded.synced_at
        """,
        tuple(
            event[key]
            for key in (
                "id", "account_id", "subject", "body_preview", "start_ts", "end_ts",
                "start_local", "end_local", "all_day", "location", "web_link",
                "series_master_id", "is_cancelled", "change_key", "synced_at",
            )
        ),
    )


def _row_to_event(row: Any) -> dict[str, Any]:
    return _event_dict(dict(row))


def _event_dict(event: Mapping[str, Any]) -> dict[str, Any]:
    account_id = str(event.get("account_id") or OUTLOOK_ACCOUNT_ID)
    return {
        "id": event["id"],
        "account_id": account_id,
        "provider": str(event.get("provider") or account_id),
        "subject": event["subject"],
        "body_preview": event["body_preview"],
        "start_ts": event["start_ts"],
        "end_ts": event["end_ts"],
        "start_local": event["start_local"],
        "end_local": event["end_local"],
        "all_day": bool(event["all_day"]),
        "location": event["location"],
        "web_link": event["web_link"],
        "series_master_id": event["series_master_id"],
        "is_cancelled": bool(event["is_cancelled"]),
        "change_key": event["change_key"],
        "synced_at": event["synced_at"],
        "goal_link": None,
        "goal_links": [],
    }


def _attach_goal_links(events: list[dict[str, Any]]) -> None:
    links = goal_schedule_service.links_for_events([str(event["id"]) for event in events])
    for event in events:
        event["goal_links"] = links.get(str(event["id"]), [])


def _meta_value(key: str) -> str | None:
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (key,))
    return str(row["value"]) if row is not None else None


def _set_meta(conn: Any, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
