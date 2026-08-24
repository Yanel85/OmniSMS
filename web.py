#!/usr/bin/env python3
"""
OmniSMS设备与通讯管理系统 v3.0.0- Air780系列设备短信通话融合管理程序
基于 FastAPI + Tailwind CSS 的 Web 管理界面
"""

import json
import logging
import os
import re
import asyncio
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from itertools import islice
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn

# 导入核心引擎
from omnisms import OmniSMSEngine, Config, setup_logging, WebSerialVirtualPort, utc_timestamp
from database import Database

# ==================== 配置 ====================
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000
LOG_DIR = "logs"                              # 日志文件目录 (与 omnisms.py 共享)
LOG_FILE = "logs/omnisms.log"                 # 当前日志文件 (按天轮转)
LOG_BACKUP_COUNT = 30                         # 保留历史日志天数

# ==================== 工具函数 ====================
def _parse_bands(raw) -> List[int]:
    """将频段字段(数据库/引擎中的 JSON 字符串或列表)解析为整型数组; 失败返回空列表。"""
    if isinstance(raw, list):
        return [int(b) for b in raw if isinstance(b, (int, float))]
    if isinstance(raw, str) and raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [int(b) for b in data if isinstance(b, (int, float))]
        except (ValueError, TypeError):
            pass
    return []


def normalize_outgoing_phone(raw: str) -> str:
    """外发号码规范化: 默认不进行 +86 国家码补全。

    仅去除常见分隔符(空格/连字符/括号/点), 并保留用户显式写出的 '+' 国际前缀;
    未带 '+' 的号码按原样下发 (不自动补 +86), 交由固件/运营商按本地规则处理。

    - '10010'        -> '10010'        (不补 +86, 原样下发)
    - '13800138000'  -> '13800138000'
    - '8610010'      -> '8610010'
    - '+1 202 555'   -> '+1202555'     (显式国际号码, 仅去除分隔符)
    """
    s = re.sub(r"[\s\-\(\)\.]", "", str(raw).strip())
    return s


def is_docker_environment() -> bool:
    """检测当前是否运行在 Docker 容器内。

    判定依据 (任一命中即视为 Docker 环境):
      - 存在 /.dockerenv 文件
      - /proc/1/cgroup 中包含 docker / kubepods / containerd 关键字
      - 环境变量 OMNISMS_FORCE_DOCKER=1 显式强制
    """
    if os.environ.get('OMNISMS_FORCE_DOCKER', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return True
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read().lower()
        if any(k in content for k in ('docker', 'kubepods', 'containerd', 'libpod')):
            return True
    except (OSError, IOError):
        pass
    return False

# 全局引擎实例
engine: Optional[OmniSMSEngine] = None

# 全局数据库实例
db: Optional[Database] = None

# 全局事件循环 (在 lifespan 启动时捕获, 供引擎业务线程安全地推送 WebSocket)
LOOP = None

# 是否运行在 Docker 容器内 (Docker 环境仅支持 WebSerial 桥接, 禁用 pySerial)
IS_DOCKER = is_docker_environment()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global LOOP
    LOOP = asyncio.get_running_loop()
    yield


# ==================== FastAPI 应用 ====================
app = FastAPI(
    title="OmniSMS",
    description="Air780系列设备短信通话融合管理程序",
    version="3.0.0",
    lifespan=lifespan,
)

# 模板和静态文件（使用绝对路径）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# WebSocket 连接管理器
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            await connection.send_json(message)

manager = ConnectionManager()

# ==================== WebSerial 桥接支持 ====================

# 全局活跃 WebSerial 桥接表: {bridge_id: WebSerialBridge}
active_bridges: Dict[str, 'WebSerialBridge'] = {}


class WebSerialBridge:
    """管理单个浏览器 WebSerial 连接与后端引擎之间的桥接。

    数据流:
      浏览器 --[WebSerial API]--> USB 设备 (物理)
      浏览器 --[/ws/webserial]--> 后端引擎 (逻辑)

    职责:
      - 接收浏览器上报的原始数据行, 喂入引擎的 WebSerialVirtualPort
      - 将引擎下发的命令通过 WebSocket 推送给浏览器, 由浏览器写入 USB
      - 管理 WebSerial 设备的生命周期 (注册/更新/注销)
    """

    def __init__(self, bridge_id: str, websocket: WebSocket, engine_instance: OmniSMSEngine):
        self.bridge_id = bridge_id
        self.ws = websocket
        self.engine = engine_instance
        self.is_connected = True

        # 关联的设备 (identify 握手成功后填充)
        self.device_id: Optional[str] = None
        self.virtual_port: Optional[WebSerialVirtualPort] = None

        logger = logging.getLogger("OmniSMS-WSBridge")
        logger.info(f"WebSerial bridge created: {bridge_id}")

    async def handle_client_message(self, msg: dict):
        """处理来自浏览器的消息"""
        msg_type = msg.get("type")

        if msg_type == "raw_line":
            # 原始数据行 -> 转发给引擎处理
            raw_line = msg.get("data", "")
            if raw_line:
                await self._feed_to_engine(raw_line)

        elif msg_type == "register":
            # 注册/心跳 (可扩展)
            logging.getLogger("OmniSMS-WSBridge").debug(f"WebSerial client register: {msg}")

        elif msg_type == "ping":
            # 心跳响应
            await self.ws.send_json({"type": "pong", "timestamp": utc_timestamp()})

    async def _feed_to_engine(self, raw_line: str):
        """将原始数据行喂入引擎 (复用 _handle_incoming_message)"""
        logger = logging.getLogger("OmniSMS-WSBridge")

        if not self.device_id or not self.virtual_port:
            # 尚未完成 identify 握手, 尝试解析 boot 事件进行自动注册
            await self._try_auto_register(raw_line)
            return

        # 已注册设备: 直接通过 virtual_port.feed_data 喂入数据
        # 引擎的 reader_loop 会从 virtual_port.readline() 读取并调用 _handle_incoming_message
        if self.virtual_port:
            self.virtual_port.feed_data(raw_line)
            logger.debug(f"WebSerial data fed to engine ({self.device_id}): {raw_line[:80]}")

    async def _try_auto_register(self, raw_line: str):
        """尝试从原始数据中解析 boot/keepalive 事件并自动注册设备"""
        logger = logging.getLogger("OmniSMS-WSBridge")

        try:
            msg = json.loads(raw_line)
            if isinstance(msg, dict) and msg.get("event") in ("boot", "keepalive"):
                imei = msg.get("imei", "")
                if len(imei) >= 15:
                    # 调用引擎的 register_webserial_device
                    device_id = self.engine.register_webserial_device(self.bridge_id, msg)

                    if device_id:
                        self.device_id = device_id
                        # 获取引擎创建的 virtual_port 并绑定 bridge
                        device = self.engine.get_device(device_id)
                        if device and isinstance(device.serial_obj, WebSerialVirtualPort):
                            self.virtual_port = device.serial_obj
                            self.virtual_port.bridge = self

                        logger.info(f"WebSerial auto-registered device: {device_id} via {self.bridge_id}")

                        # 通知前端
                        await self.ws.send_json({
                            "type": "device_registered",
                            "device_id": device_id,
                            "connection_type": "webserial",
                            "timestamp": utc_timestamp()
                        })

        except json.JSONDecodeError:
            pass  # 非 JSON 行, 忽略
        except Exception as e:
            logger.error(f"WebSerial auto-register error: {e}")

    async def poll_and_send_command(self):
        """检查引擎是否有待发送命令, 有则推送给浏览器"""
        if not self.engine or not self.device_id:
            return

        command = self.engine.get_webserial_command(self.bridge_id)
        if command:
            await self.ws.send_json({
                "type": "command",
                **command,
                "timestamp": utc_timestamp()
            })
            logging.getLogger("OmniSMS-WSBridge").debug(
                f"WebSerial command sent to browser: {command.get('action', '?')}"
            )

    def disconnect(self):
        """清理桥接资源"""
        self.is_connected = False

        # 从引擎移除关联设备
        if self.device_id:
            self.engine.unregister_webserial_device(self.bridge_id)

        logging.getLogger("OmniSMS-WSBridge").info(
            f"WebSerial bridge disconnected: {self.bridge_id} (was device: {self.device_id})"
        )


# 内存日志缓存 (供前端启动时填充; 持久化由日志文件负责)
log_cache: List[dict] = []
MAX_LOG_CACHE = 1000


# ==================== 数据模型 ====================
class SendSMSRequest(BaseModel):
    device_id: str
    phone: str = Field(..., pattern=r"^\+?[\d][\d\s\-\(\)\.]{2,19}$")
    text: str = Field(..., min_length=1, max_length=1000)  # 与固件 SMS_MAX_BYTES 对齐, 支持长短信


class MakeCallRequest(BaseModel):
    device_id: str
    phone: str = Field(..., pattern=r"^\+?[\d][\d\s\-\(\)\.]{2,19}$")


class HangupCallRequest(BaseModel):
    device_id: str


class PurgeSMSRequest(BaseModel):
    device_id: Optional[str] = None   # 为空表示跨所有设备清空该号码
    phone: str
    confirm: bool = False             # 严格的确认机制: 必须显式确认才执行实际删除
    dry_run: bool = False             # 仅统计不删除


# ==================== 初始化引擎 ====================
def init_engine(disable_pyserial: bool = False):
    """初始化 OmniSMS 引擎

    Args:
        disable_pyserial: 是否禁用 pySerial 直连 (Docker 环境为 True, 仅支持 WebSerial 桥接)
    """
    global engine, db
    config = Config()
    config.DISABLE_PYSERIAL = disable_pyserial
    setup_logging(config)
    db = Database(config.DB_PATH)
    engine = OmniSMSEngine(config)
    engine.db = db
    engine.event_callback = broadcast_engine_event
    engine.start()


# ==================== 业务事件广播 ====================
def broadcast_engine_event(event_type: str, device_id: str, data: dict):
    """引擎业务事件回调 -> 通过 WebSocket 推送到前端"""
    if event_type.startswith("sms"):
        ws_type = "sms_event"
    elif event_type.startswith("call"):
        ws_type = "call_event"
    else:
        ws_type = "device_event"
    
    payload = {
        "type": ws_type,
        "data": {
            "event": event_type,
            "device_id": device_id,
            **data
        }
    }
    
    try:
        loop = LOOP
        if loop is None:
            loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(manager.broadcast(payload))
        )
    except RuntimeError:
        pass  # 事件循环未运行时静默忽略


# ==================== 日志处理器 ====================
class WebLogHandler(logging.Handler):
    """
    自定义日志处理器:
    - 添加到内存缓存 (供前端启动时填充)
    - 通过 WebSocket 推送到前端
    - 不再写入数据库, 由文件 Handler (TimedRotatingFileHandler) 负责持久化
    """
    def emit(self, record):
        try:
            log_entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
                "module": record.module,
            }

            # 添加到内存缓存
            log_cache.append(log_entry)
            if len(log_cache) > MAX_LOG_CACHE:
                log_cache.pop(0)

            # 推送 WebSocket
            try:
                loop = LOOP
                if loop is None:
                    loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(manager.broadcast({"type": "log", "data": log_entry}))
                )
            except RuntimeError:
                pass  # 事件循环未运行时静默忽略
        except Exception:
            pass


# ==================== 日志文件读取器 ====================
class LogFileReader:
    """
    从 logs/ 目录读取日志文件, 支持:
    - 按级别 / 关键词 / 时间范围过滤
    - 分页
    - 自动按文件 mtime 倒序合并
    - 单行格式: [2026-07-16 02:34:13] INFO     [OmniSMS] message
    """
    LINE_PATTERN = re.compile(
        r'^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+'
        r'(?P<level>\w+)\s+\[(?P<logger>[^\]]+)\]\s+(?P<msg>.*)$'
    )

    def __init__(self, log_dir: str = LOG_DIR, main_file: str = LOG_FILE):
        self.log_dir = log_dir
        self.main_file = main_file

    def list_files(self):
        """列出所有日志文件 (主文件 + 历史轮转文件), 按时间倒序"""
        files = []
        main_path = os.path.join(self.log_dir, os.path.basename(self.main_file))
        if os.path.exists(main_path):
            files.append(main_path)

        # 历史文件: omnisms.log.YYYY-MM-DD 或 omnisms.log.YYYY-MM-DD_HH-MM-SS
        if os.path.isdir(self.log_dir):
            for name in os.listdir(self.log_dir):
                if name.startswith(os.path.basename(self.main_file) + "."):
                    files.append(os.path.join(self.log_dir, name))
        # 按文件名倒序 (新日期在前)
        files.sort(reverse=True)
        return files

    def parse_line(self, line: str):
        m = self.LINE_PATTERN.match(line.rstrip())
        if not m:
            return None
        return {
            "timestamp": m.group("ts"),
            "level": m.group("level"),
            "logger": m.group("logger"),
            "message": m.group("msg"),
            "module": m.group("logger"),
        }

    def _iter_records(self, level: Optional[str] = None, keyword: Optional[str] = None,
                      start_time: Optional[str] = None, end_time: Optional[str] = None):
        """生成器: 倒序产出符合条件的日志记录"""
        level = level.upper() if level else None
        kw = keyword.lower() if keyword else None
        for path in self.list_files():
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
            except Exception:
                continue
            # 单文件内倒序读取
            for raw in reversed(lines):
                entry = self.parse_line(raw)
                if not entry:
                    continue
                if level and entry["level"] != level:
                    continue
                if start_time and entry["timestamp"] < start_time:
                    continue
                if end_time and entry["timestamp"] > end_time:
                    continue
                if kw:
                    haystack = f'{entry["message"]} {entry["logger"]} {entry["module"]}'.lower()
                    if kw not in haystack:
                        continue
                yield entry

    def get_logs(self, level=None, keyword=None, start_time=None, end_time=None,
                 limit: int = 200, offset: int = 0):
        it = self._iter_records(level, keyword, start_time, end_time)
        # 跳过 offset 条后取 limit 条, 避免一次性物化全部记录
        records = list(islice(it, offset, offset + limit))
        # total 需单独统计 (生成器已消费, 重新迭代计数)
        total = sum(1 for _ in self._iter_records(level, keyword, start_time, end_time))
        return records, total

    def get_recent(self, limit: int = 200):
        """获取最近 N 条日志 (供前端启动时填充)"""
        records = []
        for entry in self._iter_records():
            records.append(entry)
            if len(records) >= limit:
                break
        return records

    def count(self) -> int:
        """统计所有日志行数 (近似, 用于前端显示总数)"""
        total = 0
        for path in self.list_files():
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    total += sum(1 for _ in f)
            except Exception:
                pass
        return total


# ==================== API 路由 ====================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """主页面"""
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/env")
async def get_env():
    """返回运行环境信息, 供前端决定连接模式。

    - is_docker: 是否运行在 Docker 容器内 (Docker 环境仅支持 WebSerial 桥接)
    - pyserial_disabled: 后端是否已禁用 pySerial 直连
    """
    return JSONResponse(content={
        "is_docker": IS_DOCKER,
        "pyserial_disabled": bool(engine and engine.config.DISABLE_PYSERIAL),
    })


@app.get("/api/devices")
async def get_devices():
    """获取所有已注册设备（合并内存在线状态 + 数据库备注/离线设备）"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    # 在线设备 (来自引擎内存, key 为 device_id = 本机号码 或 回退 IMSI, 再回退 IMEI)
    merged = {}
    for device_id, device in engine.get_all_devices().items():
        merged[device_id] = {
            "device_id": device_id,
            "phone": getattr(device, 'phone', "") or "",
            "imei": getattr(device, 'imei', None),
            "iccid": getattr(device, 'iccid', None),
            "imsi": getattr(device, 'imsi', None),
            "no_card": getattr(device, 'no_card', False),
            "csq": getattr(device, 'csq', None),
            "bands": _parse_bands(getattr(device, 'bands', None)),
            "port": getattr(device, 'at_port', None) or getattr(device, 'port', None),
            "status": device.status,
            "last_active": getattr(device, 'last_seen', None) or datetime.now(timezone.utc).isoformat(),
            "rssi": getattr(device, 'rssi', None),
            "rsrp": getattr(device, 'rsrp', None),
            "rsrq": getattr(device, 'rsrq', None),
            "snr": getattr(device, 'snr', None),
            "net_status": getattr(device, 'net_status', None),
            "series": getattr(device, 'series', "") or "",
            "model": getattr(device, 'model', "") or "",
            "connection_type": getattr(device, 'connection_type', 'pyserial'),
            "remark": "",
        }
    
    # 补充数据库中的设备 (备注 + 离线设备)
    if db is not None:
        for d in db.get_devices():
            device_id = d["device_id"]
            if device_id not in merged:
                merged[device_id] = {
                    "device_id": device_id,
                    "phone": d.get("phone") or "",
                    "imei": d.get("imei"),
                    "iccid": d.get("iccid"),
                    "imsi": d.get("imsi"),
                    "no_card": not (d.get("phone") or d.get("imsi")),
                    "csq": d.get("csq"),
                    "bands": _parse_bands(d.get("bands")),
                    "port": d.get("at_port") or d.get("log_port"),
                    "status": d.get("status", "offline"),
                    "last_active": d.get("last_seen") or datetime.now(timezone.utc).isoformat(),
                    "rssi": d.get("rssi"),
                    "rsrp": d.get("rsrp"),
                    "rsrq": d.get("rsrq"),
                    "snr": d.get("snr"),
                    "net_status": d.get("net_status"),
                    "series": d.get("series", ""),
                    "model": d.get("model", ""),
                    "remark": d.get("remark", ""),
                }
            else:
                merged[device_id]["remark"] = d.get("remark", "")
    
    return JSONResponse(content={"devices": list(merged.values())})


@app.post("/api/scan")
async def scan_devices(duration: float = 15.0):
    """手动扫描发现设备: 对每个端口独立探测, 每个端口最多等待 duration 秒(默认 15 秒)"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")

    if engine.is_scanning:
        return JSONResponse(content={
            "success": False,
            "message": "已有扫描正在进行中"
        })

    # 限制每端口探测时长在合理范围内
    duration = max(5.0, min(float(duration), 300.0))
    ok = engine.start_manual_scan(duration)
    if ok:
        return JSONResponse(content={
            "success": True,
            "message": f"已开始扫描，每个端口最多等待 {int(duration)} 秒",
            "duration": duration
        })
    return JSONResponse(content={
        "success": False,
        "message": "启动扫描失败"
    })


@app.post("/api/scan/stop")
async def stop_scan():
    """提前停止正在进行的手动扫描"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")

    ok = engine.stop_manual_scan()
    return JSONResponse(content={
        "success": ok,
        "message": "已发送停止信号" if ok else "当前没有进行中的扫描"
    })


@app.post("/api/scan/auto/start")
async def start_auto_scan():
    """启动后台自动扫描(持续发现并注册设备)"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")

    ok = engine.start_auto_scan()
    return JSONResponse(content={
        "success": ok,
        "message": "后台自动扫描已启动" if ok else "自动扫描已在运行中"
    })


@app.post("/api/scan/auto/stop")
async def stop_auto_scan():
    """停止后台自动扫描(不影响已注册设备)"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")

    ok = engine.stop_auto_scan()
    return JSONResponse(content={
        "success": ok,
        "message": "已停止自动扫描" if ok else "当前没有进行中的自动扫描"
    })


@app.get("/api/scan/status")
async def scan_status():
    """查询当前是否正在扫描(手动) / 自动扫描"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")

    return JSONResponse(content={
        "success": True,
        "scanning": engine.is_scanning,
        "auto_scanning": engine.is_auto_scanning
    })


class DisconnectDeviceRequest(BaseModel):
    device_id: str


@app.post("/api/disconnect")
async def disconnect_device(request: DisconnectDeviceRequest):
    """从引擎内存移除设备, 并从数据库中彻底删除"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    try:
        # 从引擎内存中移除 (关闭串口、清理端口映射与读取线程)
        engine.remove_device(request.device_id)

        # 从数据库中彻底删除设备记录
        if db:
            db.delete_device(request.device_id)
        
        return JSONResponse(content={
            "success": True,
            "message": f"设备 {request.device_id[:12]}... 已删除"
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs/cache")
async def get_logs_cache():
    """获取缓存的日志数据（前端启动时填充，从日志文件读取最近 200 条）"""
    try:
        reader = LogFileReader()
        logs = reader.get_recent(limit=200)
    except Exception as e:
        # 兜底: 退回到内存缓存
        logs = list(log_cache[-200:]) if log_cache else []
    total = len(logs) if logs else len(log_cache)
    return JSONResponse(content={
        "success": True,
        "logs": logs,
        "total": total
    })


@app.get("/api/devices/{device_id}")
async def get_device(device_id: str):
    """获取指定设备信息"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    device = engine.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    return JSONResponse(content={
        "success": True,
        "data": {
            "device_id": device_id,
            "phone": getattr(device, 'phone', '') or '',
            "imei": device.imei,
            "iccid": device.iccid,
            "imsi": getattr(device, 'imsi', None),
            "no_card": getattr(device, 'no_card', False),
            "at_port": device.at_port,
            "log_port": device.log_port,
            "status": device.status,
            "last_seen": device.last_seen,
            "series": getattr(device, 'series', "") or "",
            "model": getattr(device, 'model', "") or "",
            "remark": getattr(device, 'remark', "") or "",
        }
    })


@app.post("/api/sms/send")
async def send_sms(request: SendSMSRequest):
    """发送短信"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    # 无卡设备(既无号码也无 IMSI, device_id 回退 IMEI)短信功能不可用
    dev = engine.get_device(request.device_id)
    if dev and getattr(dev, 'no_card', False):
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": "设备无卡，短信功能不可用"
        })
    
    phone = normalize_outgoing_phone(request.phone)
    task_id = engine.send_sms(request.device_id, phone, request.text)
    if task_id is None:
        return JSONResponse(status_code=502, content={
            "success": False,
            "message": "短信发送失败：设备离线或命令下发失败"
        })
    return JSONResponse(content={
        "success": True,
        "message": "短信发送命令已下发",
        "data": {"task_id": task_id}
    })


@app.post("/api/sms/purge")
async def purge_sms(request: PurgeSMSRequest):
    """彻底清空指定号码的短信记录 (事务 + 二次查询验证 + 回滚)

    严格的确认机制: confirm 必须为 True 才执行实际删除, 否则返回 NEEDS_CONFIRMATION。
    返回结果含 success / status / before_count / after_count / deleted_count 等明确状态。
    """
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")

    result = db.purge_sms_by_phone(
        device_id=request.device_id,
        phone=request.phone,
        confirm=request.confirm,
        dry_run=request.dry_run,
    )
    # 验证失败 / 异常 / 参数错误 -> 409; 其余 (成功/预览/需确认) -> 200
    http_status = 409 if result["status"] in ("FAILED_VERIFICATION", "ERROR", "INVALID_PARAM") else 200
    return JSONResponse(status_code=http_status, content=result)


# ==================== 设备备注 ====================

class RemarkRequest(BaseModel):
    device_id: str
    remark: str = ""


@app.post("/api/devices/remark")
async def save_device_remark(request: RemarkRequest):
    """保存设备备注到数据库"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    db.update_device_remark(request.device_id, request.remark)
    
    # 同步更新引擎内存中的设备备注字段(若有)
    if engine:
        dev = engine.get_device(request.device_id)
        if dev:
            dev.remark = request.remark
    
    return JSONResponse(content={"success": True, "message": "备注已保存"})


# ==================== 短信记录 ====================

@app.get("/api/sms/conversations")
async def get_sms_conversations(device_id: str = Query(..., description="设备标识(device_id)")):
    """获取某设备的全部短信记录 (扁平, 原样返回 peer_phone; 聚合与展示格式化由前端完成)"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    messages = db.get_sms_all(device_id)
    return JSONResponse(content={"success": True, "messages": messages})


@app.get("/api/sms/messages")
async def get_sms_messages(
    device_id: str = Query(..., description="设备标识(device_id)"),
    peer_phone: str = Query(..., description="对方号码")
):
    """获取某会话的全部短信消息"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    messages = db.get_sms_messages(device_id, peer_phone)
    return JSONResponse(content={"success": True, "messages": messages})


# ==================== 通话记录 ====================

@app.get("/api/calls")
async def get_call_records(device_id: str = Query(..., description="设备标识(device_id)")):
    """获取某设备的通话记录"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    records = db.get_calls(device_id)
    return JSONResponse(content={"success": True, "records": records})


@app.get("/api/calls/conversations")
async def get_call_conversations(device_id: str = Query(..., description="设备标识(device_id)")):
    """获取某设备的通话记录 (扁平, 原样返回 peer_phone; 聚合与展示格式化由前端完成)"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    records = db.get_calls(device_id)
    return JSONResponse(content={"success": True, "conversations": records})


@app.post("/api/call/make")
@app.post("/api/call/dial")
async def make_call(request: MakeCallRequest):
    """拨打电话"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    # 无卡设备(既无号码也无 IMSI, device_id 回退 IMEI)通话功能不可用
    dev = engine.get_device(request.device_id)
    if dev and getattr(dev, 'no_card', False):
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": "设备无卡，通话功能不可用"
        })
    
    phone = normalize_outgoing_phone(request.phone)
    success = engine.make_call(request.device_id, phone)
    if success:
        return JSONResponse(content={
            "success": True,
            "message": "拨号命令已下发"
        })
    else:
        raise HTTPException(status_code=500, detail="拨号失败")


@app.post("/api/call/hangup")
async def hangup_call(request: HangupCallRequest):
    """挂断电话"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    # 无卡设备通话功能不可用
    dev = engine.get_device(request.device_id)
    if dev and getattr(dev, 'no_card', False):
        return JSONResponse(status_code=400, content={
            "success": False,
            "message": "设备无卡，通话功能不可用"
        })
    
    success = engine.hangup_call(request.device_id)
    if success:
        return JSONResponse(content={
            "success": True,
            "message": "挂断命令已下发"
        })
    else:
        raise HTTPException(status_code=500, detail="挂断失败")


@app.get("/api/logs")
async def get_logs(
    level: Optional[str] = Query(None, description="日志级别过滤"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    start_time: Optional[str] = Query(None, description="起始时间 (YYYY-MM-DD HH:MM:SS)"),
    end_time: Optional[str] = Query(None, description="结束时间 (YYYY-MM-DD HH:MM:SS)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200)
):
    """获取历史日志（从 logs/ 目录的文件读取，支持过滤与分页）"""
    offset = (page - 1) * page_size
    reader = LogFileReader()
    logs, total = reader.get_logs(
        level=level, keyword=keyword,
        start_time=start_time, end_time=end_time,
        limit=page_size, offset=offset
    )
    return JSONResponse(content={
        "success": True,
        "data": {
            "logs": logs,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    })


@app.get("/api/logs/files")
async def get_log_files():
    """列出所有日志文件 (主文件 + 轮转历史), 用于前端展示"""
    reader = LogFileReader()
    files = []
    for path in reader.list_files():
        try:
            stat = os.stat(path)
            files.append({
                "name": os.path.basename(path),
                "path": path,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception:
            pass
    return JSONResponse(content={"success": True, "files": files})


@app.post("/api/logs/clear-cache")
async def clear_log_cache():
    """清理内存日志缓存 (不影响磁盘文件)"""
    log_cache.clear()
    return JSONResponse(content={"success": True, "message": "内存日志缓存已清空"})


@app.websocket("/ws/log")
async def websocket_log(websocket: WebSocket):
    """WebSocket 实时日志推送"""
    await manager.connect(websocket)
    try:
        while True:
            # 保持连接活跃
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.websocket("/ws/webserial")
async def websocket_webserial_bridge(websocket: WebSocket):
    """WebSerial 桥接 WebSocket 端点。

    浏览器通过此端点将 WebSerial API 收到的原始数据转发到后端,
    后端通过此端点将下行命令下发给浏览器写入 USB。

    协议:
      浏览器 -> 后端: {"type": "raw_line", "data": "<原始JSON行>"}
      后端 -> 浏览器: {"type": "command", "action": "...", ...}
      浏览器 -> 后端: {"type": "register", ...}
      后端 -> 浏览器: {"type": "registered", "bridge_id": "..."}
    """
    global active_bridges

    await manager.connect(websocket)
    bridge_id = f"ws-{uuid.uuid4().hex[:12]}"
    bridge = WebSerialBridge(bridge_id, websocket, engine)

    active_bridges[bridge_id] = bridge  # 全局注册
    ws_logger = logging.getLogger("OmniSMS-WSBridge")

    try:
        ws_logger.info(f"WebSerial bridge connected: {bridge_id}")

        # 发送注册确认
        await websocket.send_json({
            "type": "registered",
            "bridge_id": bridge_id,
            "status": "ok",
            "timestamp": utc_timestamp()
        })

        # 消息处理循环 (同时轮询待发送命令)
        while True:
            try:
                # 使用 asyncio.wait_for 实现带超时的接收, 以便定期检查命令队列
                raw_message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=0.5
                )

                try:
                    msg = json.loads(raw_message)
                    await bridge.handle_client_message(msg)
                except json.JSONDecodeError:
                    ws_logger.warning(f"Invalid JSON from WebSerial client: {raw_message[:100]}")
                except Exception as e:
                    ws_logger.error(f"Error handling WebSerial message: {e}")

            except asyncio.TimeoutError:
                # 超时 -> 轮询命令队列
                pass

            # 检查是否有待发送的命令
            await bridge.poll_and_send_command()

    except WebSocketDisconnect:
        ws_logger.info(f"WebSerial bridge disconnected: {bridge_id}")
    except Exception as e:
        ws_logger.error(f"WebSerial bridge error ({bridge_id}): {e}")
    finally:
        bridge.disconnect()
        active_bridges.pop(bridge_id, None)
        manager.disconnect(websocket)


# ==================== 启动入口 ====================
if __name__ == "__main__":
    import argparse
    import os as _os
    
    # 命令行参数解析
    parser = argparse.ArgumentParser(description="OmniSMS设备与通讯管理系统 - Web 管理界面")
    parser.add_argument("--host", type=str, default=WEB_HOST, help="Web 服务监听地址")
    parser.add_argument("--port", type=int, default=WEB_PORT, help="Web 服务端口")
    parser.add_argument("--ssl-cert", type=str, default=None, help="SSL 证书文件路径 (启用 HTTPS)")
    parser.add_argument("--ssl-key", type=str, default=None, help="SSL 私钥文件路径 (启用 HTTPS)")
    args = parser.parse_args()
    
    # 应用配置
    WEB_HOST = args.host
    WEB_PORT = args.port
    
    # SSL/HTTPS 配置 (支持命令行参数或环境变量)
    ssl_certfile = args.ssl_cert or _os.environ.get('SSL_CERT_FILE')
    ssl_keyfile = args.ssl_key or _os.environ.get('SSL_KEY_FILE')
    
    # 判断是否启用 HTTPS
    https_enabled = bool(ssl_certfile and ssl_keyfile and _os.path.exists(ssl_certfile) and _os.path.exists(ssl_keyfile))
    
    # 是否禁用 pySerial:
    #   1. Docker 环境时自动禁用 (仅支持 WebSerial 桥接)
    #   2. 显式设置环境变量 OMNISMS_DISABLE_PYSERIAL=1 时禁用
    disable_pyserial = IS_DOCKER or _os.environ.get('OMNISMS_DISABLE_PYSERIAL', '').strip().lower() in ('1', 'true', 'yes', 'on')
    
    print("=" * 60)
    print("OmniSMS设备与通讯管理系统 -  Web Interface Starting...")
    
    if https_enabled:
        print(f"Access: https://{WEB_HOST}:{WEB_PORT}")
        print(f"SSL Certificate: {ssl_certfile}")
        print("⚠️  使用自签名证书，浏览器会提示不安全，请选择'继续访问'")
    else:
        print(f"Access: http://{WEB_HOST}:{WEB_PORT}")
        print("💡 提示: 使用 --ssl-cert 和 --ssl-key 参数启用 HTTPS")
    
    if IS_DOCKER:
        print("🐳 Docker 环境: pySerial 已禁用, 仅支持 WebSerial 桥接")
    elif disable_pyserial:
        print("🔌 pySerial 已禁用 (OMNISMS_DISABLE_PYSERIAL=1), 仅使用 WebSerial 桥接")
    
    print("=" * 60)
    
    # 获取 logger
    logger = logging.getLogger("OmniSMS")
    
    # 初始化引擎
    init_engine(disable_pyserial=disable_pyserial)
    
    # 配置日志处理器
    web_handler = WebLogHandler()
    web_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s")
    web_handler.setFormatter(formatter)
    logging.getLogger().addHandler(web_handler)
    
    # 启动服务 (支持 HTTP/HTTPS)
    if ssl_certfile and ssl_keyfile and _os.path.exists(ssl_certfile) and _os.path.exists(ssl_keyfile):
        uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, 
                    ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
    else:
        uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
