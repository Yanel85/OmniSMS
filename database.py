#!/usr/bin/env python3
"""
database.py - OmniSMS SQLite 持久化层
保存设备信息、短信记录与通话记录 (系统日志改由日志文件持久化)
线程安全: 每次操作独立连接 + 全局锁串行化
"""

import re
import sqlite3
import threading
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

logger = logging.getLogger("OmniSMS-DB")

UTC_TZ = timezone.utc


def utc_timestamp() -> str:
    """统一时间戳: UTC ISO 8601 (带 +00:00 时区后缀)。

    与 omnisms.py 保持一致, 所有持久化时间均以此为准, 前端渲染时换算为浏览器本地时区。
    """
    return datetime.now(UTC_TZ).isoformat(timespec="seconds")


# ==================== 号码标准化 ====================
# 后端仅保留 normalize_phone(), 用于 purge 时按归一化键匹配同一联系人的多种原始号码形态。
# 注意: 入库存储一律原样保留 peer_phone(含原始国家码, 不做剥离), 数据完整性由后端保证;
#       号码的会话聚合与展示格式化(国家码剥离/补全)已移至前端实现。

def normalize_phone(raw) -> str:
    """将号码标准化为会话聚合匹配用的规范形式。

    规则:
      - 去除空格、连字符、括号、点号等分隔符;
      - 去除前导 '+' 或国际接入前缀 '00';
      - 中国国家码 86: 显式国际前缀(+/00)或 '86'+11位手机号(1 开头) 时剥离;
      - 结果仅保留数字。非数字号码(如字母短号)原样返回。
    保证: 同一真实号码无论是否带 +86, 归一化后得到相同的键。
    """
    if not raw:
        return raw or ""
    original = str(raw).strip()
    # 去除常见分隔符
    s = re.sub(r"[\s\-\(\)\.]", "", original)
    explicit_intl = s.startswith("+") or s.startswith("00")
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return original  # 非数字号码原样保留
    # 剥离中国国家码 86
    if digits.startswith("86"):
        rest = digits[2:]
        if explicit_intl and rest:
            # 显式国际前缀(+86 / 0086) -> 一律剥离国家码
            digits = rest
        elif len(digits) == 13 and rest.startswith("1"):
            # 无前缀但形如 86 + 11位手机号 -> 剥离国家码
            digits = rest
    return digits


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT PRIMARY KEY,   -- 业务主键: 本机号码优先, 缺失时回退 IMSI(卡的标识), 再回退 IMEI
    phone      TEXT DEFAULT '',    -- 真实本机号码(MSISDN), 可能为空
    imei       TEXT DEFAULT '',
    iccid      TEXT DEFAULT '',
    at_port    TEXT DEFAULT '',
    log_port   TEXT DEFAULT '',
    status     TEXT DEFAULT 'offline',
    remark     TEXT DEFAULT '',
    last_seen  TEXT,
    rssi       INTEGER,
    rsrp       INTEGER,
    rsrq       INTEGER,
    snr        INTEGER,
    net_status INTEGER,
    imsi       TEXT,
    csq        INTEGER,
    bands      TEXT,
    series     TEXT DEFAULT '',
    model      TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS sms_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,     -- 关联 devices.device_id (本机号码或 IMEI 兜底)
    peer_phone  TEXT NOT NULL,     -- 对方号码
    text        TEXT NOT NULL,
    direction   TEXT NOT NULL,     -- 'in' (接收) / 'out' (发送)
    status      TEXT DEFAULT 'pending',  -- pending/sent/failed/received
    task_id     TEXT,
    timestamp   TEXT NOT NULL,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS call_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    peer_phone  TEXT NOT NULL,
    direction   TEXT NOT NULL,     -- 'in' (来电) / 'out' (去电)
    status      TEXT DEFAULT 'unknown',  -- ringing/dialing/connected/disconnected/missed
    start_time  TEXT,
    end_time    TEXT,
    duration    INTEGER DEFAULT 0,  -- 秒
    created_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_sms_device_id ON sms_messages(device_id);
CREATE INDEX IF NOT EXISTS idx_sms_peer_phone ON sms_messages(peer_phone);
CREATE INDEX IF NOT EXISTS idx_sms_ts ON sms_messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_call_device_id ON call_records(device_id);
CREATE INDEX IF NOT EXISTS idx_call_ts ON call_records(start_time);
"""


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _ensure_device_columns(conn, columns: tuple):
    """幂等地为 devices 表补齐缺失列(用于旧库平滑升级, 不破坏已有数据)。"""
    if not _table_exists(conn, "devices"):
        return
    existing = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    for col in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {col} TEXT DEFAULT ''")
            logger.warning("devices 表补齐缺失列: %s", col)


def _has_legacy_schema(conn) -> bool:
    """检测旧版 schema: sms_messages 仍使用 imei 列(而非 device_id)。"""
    if not _table_exists(conn, "sms_messages"):
        return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sms_messages)")}
    return "imei" in cols


def _migrate_legacy_to_device_id(conn):
    """把以 IMEI 为主键的旧表重建为以 device_id(本机号码优先) 为主键。

    旧数据以 imei 作为 device_id 保留(历史短信/通话仍可关联),
    旧表重命名为 _legacy_* 备份以供人工核对。
    """
    legacy_tables = [t for t in ("devices", "sms_messages", "call_records")
                     if _table_exists(conn, t)]
    for t in legacy_tables:
        conn.execute(f"ALTER TABLE {t} RENAME TO _legacy_{t}")

    conn.executescript(SCHEMA)

    if _table_exists(conn, "_legacy_devices"):
        conn.execute(
            """INSERT OR IGNORE INTO devices
               (device_id, imei, iccid, at_port, log_port, status, remark, last_seen,
                rssi, rsrp, rsrq, snr, net_status, created_at)
               SELECT imei, imei, iccid, at_port, log_port, status, remark, last_seen,
                rssi, rsrp, rsrq, snr, net_status, created_at FROM _legacy_devices"""
        )
    if _table_exists(conn, "_legacy_sms_messages"):
        conn.execute(
            """INSERT OR IGNORE INTO sms_messages
               (device_id, peer_phone, text, direction, status, task_id, timestamp, created_at)
               SELECT imei, phone, text, direction, status, task_id, timestamp, created_at
               FROM _legacy_sms_messages"""
        )
    if _table_exists(conn, "_legacy_call_records"):
        conn.execute(
            """INSERT OR IGNORE INTO call_records
               (device_id, peer_phone, direction, status, start_time, end_time, duration, created_at)
               SELECT imei, phone, direction, status, start_time, end_time, duration, created_at
               FROM _legacy_call_records"""
        )
    conn.commit()


class Database:
    """OmniSMS SQLite 数据库封装 (线程安全)"""

    def __init__(self, db_path: str = "omnisms.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                if _has_legacy_schema(conn):
                    logger.warning("检测到旧版 IMEI 主键 schema, 正在重建为 device_id(本机号码) 主键并迁移历史数据...")
                    _migrate_legacy_to_device_id(conn)
                    logger.warning("schema 迁移完成, 旧表已备份为 _legacy_*")
                else:
                    conn.executescript(SCHEMA)
                # 兼容旧库: 若 devices 表缺少 series/model 列则补齐(幂等)
                _ensure_device_columns(conn, ("series", "model"))
                conn.commit()
            finally:
                conn.close()

    # ==================== 设备 ====================

    def upsert_device(self, device_id: str, phone: str = "", imei: str = "", iccid: str = "",
                      at_port: str = "", log_port: str = "", status: str = "online",
                      remark: str = "", last_seen: Optional[str] = None,
                      rssi: Optional[int] = None, rsrp: Optional[int] = None,
                      rsrq: Optional[int] = None, snr: Optional[int] = None,
                      net_status: Optional[int] = None,
                      imsi: Optional[str] = None, csq: Optional[int] = None,
                      bands: Optional[str] = None,
                      series: str = "", model: str = ""):
        now = utc_timestamp()
        last_seen = last_seen or now
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO devices (device_id, phone, imei, iccid, at_port, log_port, status, remark, last_seen, rssi, rsrp, rsrq, snr, net_status, imsi, csq, bands, series, model, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(device_id) DO UPDATE SET
                           phone=COALESCE(excluded.phone, devices.phone),
                           imei=COALESCE(excluded.imei, devices.imei),
                           iccid=excluded.iccid,
                           at_port=COALESCE(excluded.at_port, at_port),
                           log_port=COALESCE(excluded.log_port, log_port),
                           status=excluded.status,
                           last_seen=excluded.last_seen,
                           rssi=COALESCE(excluded.rssi, devices.rssi),
                           rsrp=COALESCE(excluded.rsrp, devices.rsrp),
                           rsrq=COALESCE(excluded.rsrq, devices.rsrq),
                           snr=COALESCE(excluded.snr, devices.snr),
                           net_status=COALESCE(excluded.net_status, devices.net_status),
                           imsi=COALESCE(excluded.imsi, devices.imsi),
                           csq=COALESCE(excluded.csq, devices.csq),
                           bands=COALESCE(excluded.bands, devices.bands),
                           series=COALESCE(excluded.series, devices.series),
                           model=COALESCE(excluded.model, devices.model)""",
                    (device_id, phone, imei, iccid, at_port, log_port, status, remark, last_seen, rssi, rsrp, rsrq, snr, net_status, imsi, csq, bands, series, model, now)
                )
                conn.commit()
            finally:
                conn.close()

    def update_device_status(self, device_id: str, status: str, last_seen: Optional[str] = None,
                             phone: Optional[str] = None, imei: Optional[str] = None,
                             rssi: Optional[int] = None, rsrp: Optional[int] = None,
                             rsrq: Optional[int] = None, snr: Optional[int] = None,
                             net_status: Optional[int] = None,
                             imsi: Optional[str] = None, csq: Optional[int] = None,
                             bands: Optional[str] = None,
                             series: Optional[str] = None, model: Optional[str] = None):
        last_seen = last_seen or utc_timestamp()
        with self._lock:
            conn = self._get_conn()
            try:
                values = {
                    "phone": phone, "imei": imei,
                    "rssi": rssi, "rsrp": rsrp, "rsrq": rsrq,
                    "snr": snr, "net_status": net_status,
                    "imsi": imsi, "csq": csq, "bands": bands,
                    "series": series, "model": model,
                }
                updates = ["status=?", "last_seen=?"]
                params = [status, last_seen]
                for column, value in values.items():
                    if value is not None:
                        updates.append(f"{column}=?")
                        params.append(value)
                params.append(device_id)
                conn.execute(
                    f"UPDATE devices SET {', '.join(updates)} WHERE device_id=?",
                    params
                )
                conn.commit()
            finally:
                conn.close()

    def update_device_remark(self, device_id: str, remark: str):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE devices SET remark=? WHERE device_id=?",
                    (remark, device_id)
                )
                conn.commit()
            finally:
                conn.close()

    def get_devices(self) -> List[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT device_id, phone, imei, iccid, at_port, log_port, status, remark, last_seen, rssi, rsrp, rsrq, snr, net_status, imsi, csq, bands, series, model FROM devices ORDER BY last_seen DESC"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_device(self, device_id: str) -> Optional[Dict]:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT device_id, phone, imei, iccid, at_port, log_port, status, remark, last_seen, rssi, rsrp, rsrq, snr, net_status, imsi, csq, bands, series, model FROM devices WHERE device_id=?",
                    (device_id,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def delete_device(self, device_id: str):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
                conn.commit()
            finally:
                conn.close()

    # ==================== 短信 ====================

    def add_sms(self, device_id: str, peer_phone: str, text: str, direction: str,
                status: str = "pending", task_id: Optional[str] = None,
                timestamp: Optional[str] = None) -> int:
        # 原样存储对方号码, 不做归一化/剥离国家码, 保证后端数据完整性
        now = utc_timestamp()
        ts = timestamp or now
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO sms_messages (device_id, peer_phone, text, direction, status, task_id, timestamp, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (device_id, peer_phone, text, direction, status, task_id, ts, now)
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_sms_status(self, task_id: str, status: str):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE sms_messages SET status=? WHERE task_id=?",
                    (status, task_id)
                )
                conn.commit()
            finally:
                conn.close()

    def get_sms_all(self, device_id: str) -> List[Dict]:
        """获取某设备的全部短信记录 (扁平, 原样返回 peer_phone, 不做聚合/格式化)。

        号码的聚合(按归一化键分组)与展示格式化(国家码剥离/补全)交由前端完成,
        后端仅负责持久化原始数据, 保证数据完整性。
        """
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT id, peer_phone, text, direction, status, timestamp FROM sms_messages WHERE device_id=? ORDER BY id ASC",
                    (device_id,)
                ).fetchall()
                result = []
                dir_map = {"out": "sent", "in": "received"}
                for r in rows:
                    result.append({
                        "id": r["id"],
                        "peer_phone": r["peer_phone"],
                        "text": r["text"],
                        "direction": dir_map.get(r["direction"], r["direction"]),
                        "status": r["status"],
                        "time": r["timestamp"],
                    })
                return result
            finally:
                conn.close()

    def get_sms_messages(self, device_id: str, peer_phone: str) -> List[Dict]:
        """获取某会话的全部消息 (按时间升序, 按原始 peer_phone 精确匹配)。"""
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT id, peer_phone, text, direction, status, timestamp FROM sms_messages WHERE device_id=? AND peer_phone=? ORDER BY id ASC",
                    (device_id, peer_phone)
                ).fetchall()
                result = []
                dir_map = {"out": "sent", "in": "received"}
                for r in rows:
                    result.append({
                        "id": r["id"],
                        "peer_phone": r["peer_phone"],
                        "text": r["text"],
                        "direction": dir_map.get(r["direction"], r["direction"]),
                        "status": r["status"],
                        "time": r["timestamp"],
                    })
                return result
            finally:
                conn.close()

    def purge_sms_by_phone(self, device_id: Optional[str], phone: str,
                           confirm: bool = False, dry_run: bool = False) -> Dict:
        """彻底清空与指定号码相关的所有短信记录 (事务 + 二次验证 + 回滚)。

        完整性保障:
          1. 后端按原始 peer_phone 存储(不剥离国家码); 此处按 normalize_phone 归一化键匹配,
             以兼容同一联系人的多种原始号码形态(如 +8610010 与 10010)一并清空;
          2. 在全局锁内的单连接事务中执行: 收集匹配行 -> 删除 -> 二次扫描验证;
          3. 校验删除行数 == 删除前计数 且 删除后计数为 0; 任一不满足则 ROLLBACK;
          4. 任何异常均 ROLLBACK 并返回明确失败状态, 杜绝部分删除/残留的不确定性。

        参数:
          device_id: 设备标识; 为 None/空 时跨所有设备清空该号码。
          phone:     目标号码 (支持带/不带 +86 及分隔符)。
          confirm:   必须为 True 才执行实际删除; 否则仅返回待删计数并提示需确认。
          dry_run:   为 True 时只统计不删除 (与 confirm 互斥, 优先)。

        返回: 含 success / status / message / before_count / after_count / deleted_count 的状态字典。
        """
        norm_phone = normalize_phone(phone)
        result: Dict = {
            "success": False,
            "status": "ERROR",
            "device_id": device_id,
            "phone": phone,
            "normalized_phone": norm_phone,
            "before_count": 0,
            "after_count": -1,
            "deleted_count": 0,
            "message": "",
        }
        if not norm_phone:
            result["status"] = "INVALID_PARAM"
            result["message"] = "phone 不能为空或无法解析"
            return result

        with self._lock:
            conn = self._get_conn()
            try:
                # 1) 收集匹配行: 按归一化键匹配, 兼容同一联系人的多种原始号码形态
                if device_id:
                    rows = conn.execute(
                        "SELECT id, peer_phone FROM sms_messages WHERE device_id=?", (device_id,)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, peer_phone FROM sms_messages"
                    ).fetchall()
                target_ids = [r["id"] for r in rows
                              if normalize_phone(r["peer_phone"]) == norm_phone]
                before = len(target_ids)
                result["before_count"] = before

                if before == 0:
                    result["success"] = True
                    result["status"] = "NO_RECORDS"
                    result["message"] = "目标号码无短信记录, 无需删除"
                    conn.commit()
                    return result

                # 2) 确认 / 预演机制: 未确认或 dry_run 不执行实际删除
                if dry_run or not confirm:
                    result["success"] = dry_run
                    result["status"] = "DRY_RUN" if dry_run else "NEEDS_CONFIRMATION"
                    result["message"] = (
                        f"将删除 {before} 条记录"
                        + (" (dry_run, 未实际执行)" if dry_run else " (需 confirm=True 才会执行)")
                    )
                    conn.rollback()
                    return result

                # 3) 事务中执行删除 (按主键精确删除匹配行)
                placeholders = ",".join("?" * len(target_ids))
                cur = conn.execute(
                    f"DELETE FROM sms_messages WHERE id IN ({placeholders})", target_ids
                )
                deleted = cur.rowcount
                result["deleted_count"] = deleted

                # 4) 二次扫描验证: 确认无归一化键匹配的残留行
                if device_id:
                    recheck = conn.execute(
                        "SELECT peer_phone FROM sms_messages WHERE device_id=?", (device_id,)
                    ).fetchall()
                else:
                    recheck = conn.execute("SELECT peer_phone FROM sms_messages").fetchall()
                after = sum(1 for r in recheck
                            if normalize_phone(r["peer_phone"]) == norm_phone)
                result["after_count"] = after

                if after == 0 and deleted == before:
                    conn.commit()
                    result["success"] = True
                    result["status"] = "SUCCESS"
                    result["message"] = f"已彻底清空 {deleted} 条短信记录, 二次验证通过"
                else:
                    conn.rollback()
                    result["success"] = False
                    result["status"] = "FAILED_VERIFICATION"
                    result["message"] = (
                        f"删除不完整: 删除前={before}, 实际删除={deleted}, 残留={after}, 已回滚"
                    )
                return result
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                result["status"] = "ERROR"
                result["message"] = f"删除过程异常, 已回滚: {e}"
                logger.exception("purge_sms_by_phone 执行异常")
                return result
            finally:
                conn.close()

    # ==================== 通话 ====================

    def add_call(self, device_id: str, peer_phone: str, direction: str,
                 status: str = "unknown", start_time: Optional[str] = None) -> int:
        # 原样存储对方号码, 不做归一化/剥离国家码, 保证后端数据完整性
        now = utc_timestamp()
        st = start_time or now
        with self._lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO call_records (device_id, peer_phone, direction, status, start_time, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (device_id, peer_phone, direction, status, st, now)
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def update_call(self, call_id: int, status: Optional[str] = None,
                    end_time: Optional[str] = None, duration: Optional[int] = None):
        with self._lock:
            conn = self._get_conn()
            try:
                if end_time is not None and duration is not None:
                    conn.execute(
                        "UPDATE call_records SET status=?, end_time=?, duration=? WHERE id=?",
                        (status, end_time, duration, call_id)
                    )
                elif status is not None:
                    conn.execute(
                        "UPDATE call_records SET status=? WHERE id=?",
                        (status, call_id)
                    )
                conn.commit()
            finally:
                conn.close()

    def get_calls(self, device_id: str) -> List[Dict]:
        """获取某设备的全部通话记录 (扁平, 原样返回 peer_phone, 不做聚合/格式化)。

        号码的聚合(按归一化键分组)与展示格式化交由前端完成。
        """
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT id, peer_phone, direction, status, start_time, end_time, duration FROM call_records WHERE device_id=? ORDER BY id DESC",
                    (device_id,)
                ).fetchall()
                result = []
                dir_map = {"in": "incoming", "out": "outgoing"}
                for r in rows:
                    result.append({
                        "id": r["id"],
                        "peer_phone": r["peer_phone"],
                        "type": dir_map.get(r["direction"], r["direction"]),
                        "status": r["status"],
                        "time": r["start_time"],
                        "start_time": r["start_time"],
                        "end_time": r["end_time"],
                        "duration": r["duration"] or 0,
                    })
                return result
            finally:
                conn.close()


