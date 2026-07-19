#!/usr/bin/env python3
"""
OmniSMS Web Management Interface
基于 FastAPI + Tailwind CSS 的 Web 管理界面
"""

import json
import logging
import os
import re
import asyncio
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn

# 导入核心引擎
from omnisms import OmniSMSEngine, Config, setup_logging
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

# ==================== FastAPI 应用 ====================
app = FastAPI(
    title="OmniSMS Web Manager",
    description="OmniSMS 设备管理界面",
    version="1.0.0"
)

# 模板和静态文件（使用绝对路径）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# 捕获运行中的事件循环, 供引擎业务线程 (串口读取线程) 安全地推送 WebSocket
@app.on_event("startup")
async def _capture_event_loop():
    global LOOP
    LOOP = asyncio.get_running_loop()

# 全局引擎实例
engine: Optional[OmniSMSEngine] = None

# 全局数据库实例
db: Optional[Database] = None

# 全局事件循环 (在 startup 中捕获, 供引擎业务线程安全地推送 WebSocket)
LOOP = None

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
        for connection in self.active_connections:
            await connection.send_json(message)

manager = ConnectionManager()

# 内存日志缓存 (供前端启动时填充; 持久化由日志文件负责)
log_cache: List[dict] = []
MAX_LOG_CACHE = 1000


# ==================== 数据模型 ====================
class SendSMSRequest(BaseModel):
    device_id: str
    phone: str = Field(..., pattern=r"^\+?\d{6,15}$")
    text: str = Field(..., min_length=1, max_length=1000)  # 与固件 SMS_MAX_BYTES 对齐, 支持长短信


class MakeCallRequest(BaseModel):
    device_id: str
    phone: str = Field(..., pattern=r"^\+?\d{6,15}$")


class HangupCallRequest(BaseModel):
    device_id: str


# ==================== 初始化引擎 ====================
def init_engine():
    """初始化 OmniSMS 引擎"""
    global engine, db
    config = Config()
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
        loop = LOOP if LOOP is not None else asyncio.get_event_loop()
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
                loop = LOOP if LOOP is not None else asyncio.get_running_loop()
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
        records = list(self._iter_records(level, keyword, start_time, end_time))
        total = len(records)
        return records[offset:offset + limit], total

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
            "at_port": device.at_port,
            "log_port": device.log_port,
            "status": device.status,
            "last_seen": device.last_seen,
            "series": getattr(device, 'series', "") or "",
            "model": getattr(device, 'model', "") or "",
        }
    })


@app.post("/api/sms/send")
async def send_sms(request: SendSMSRequest):
    """发送短信"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    task_id = engine.send_sms(request.device_id, request.phone, request.text)
    return JSONResponse(content={
        "success": True,
        "message": "短信发送命令已下发",
        "data": {"task_id": task_id}
    })


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
    """获取某设备的短信会话列表 (按号码分组)"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    conversations = db.get_sms_conversations(device_id)
    return JSONResponse(content={"success": True, "conversations": conversations})


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
    """获取某设备的通话会话列表 (按号码分组聚合)"""
    if db is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    
    conversations = db.get_call_conversations(device_id)
    return JSONResponse(content={"success": True, "conversations": conversations})


@app.post("/api/call/make")
@app.post("/api/call/dial")
async def make_call(request: MakeCallRequest):
    """拨打电话"""
    if not engine:
        raise HTTPException(status_code=500, detail="Engine not initialized")
    
    success = engine.make_call(request.device_id, request.phone)
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


# ==================== 启动入口 ====================
if __name__ == "__main__":
    import argparse
    
    # 命令行参数解析
    parser = argparse.ArgumentParser(description="OmniSMS Web Interface")
    parser.add_argument("--host", type=str, default=WEB_HOST, help="Web 服务监听地址")
    parser.add_argument("--port", type=int, default=WEB_PORT, help="Web 服务端口")
    args = parser.parse_args()
    
    # 应用配置
    WEB_HOST = args.host
    WEB_PORT = args.port
    
    print("=" * 60)
    print("OmniSMS Web Interface Starting...")
    print(f"Access: http://{WEB_HOST}:{WEB_PORT}")
    print("=" * 60)
    
    # 获取 logger
    logger = logging.getLogger("OmniSMS")
    
    # 初始化引擎
    init_engine()
    
    # 配置日志处理器
    web_handler = WebLogHandler()
    web_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s")
    web_handler.setFormatter(formatter)
    logging.getLogger().addHandler(web_handler)
    
    # 启动服务
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
