#!/usr/bin/env python3
"""
OmniSMS Linux 主机守护进程
功能: 动态发现 Air780 家族模组、管理多串口通信、处理 JSON 协议
依赖: pyserial, threading
"""

import json
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Callable, ClassVar

import serial
import serial.tools.list_ports

# SQLite 持久化层
from database import Database

# ==================== 全局配置 ====================
@dataclass
class Config:
    """全局配置类"""
    BAUD_RATE: int = 115200
    SCAN_INTERVAL_SEC: float = 3.0          # 端口扫描间隔(秒)
    SERIAL_TIMEOUT: float = 1.0             # 串口读取超时(秒)
    BOOT_TIMEOUT: float = 5.0               # 启动事件等待超时(秒)
    BOOT_RETRY_TIMEOUT: float = 10.0        # 历史保留字段(单端口探测不再额外追加该时长)
    IDENTIFY_INTERVAL_SEC: float = 3.0      # 探测期间重复发送 identify 的间隔(秒)
    MANUAL_PROBE_TIMEOUT: float = 15.0      # 手动扫描: 每个端口独立等待固件事件的超时(秒)
    RESCAN_KNOWN_GROUP_SEC: float = 60.0    # 已确定但注册失败的设备组, 降低频率重新探测的间隔(秒)
    AUTO_SCAN_STOP_AFTER_DEVICE_SEC: float = 60.0  # 自动扫描发现设备后额外运行的最大宽限秒数(<=0 表示不自动停止)

    # 判定端口运行 OmniSMS 协议的事件类型集合。
    # 任一事件出现即确认该口为固件数据口(非 Lua REPL / AT 口),
    # 命中后补发 identify 并延长等待, 争取拿到含 imei 的 boot。
    FIRMWARE_EVENTS: ClassVar[set] = {"boot", "keepalive", "log", "heartbeat", "status"}
    LOG_DIR: str = "logs"                    # 日志文件目录
    LOG_FILE: str = "logs/omnisms.log"       # 主日志文件路径(按天轮转)
    LOG_BACKUP_COUNT: int = 30               # 保留历史日志天数
    DB_PATH: str = "omnisms.db"              # SQLite 数据库路径

    # Air780E USB VID/PID 过滤 (19d1:0001)
    LUAT_VID: int = 0x19D1
    LUAT_PID: int = 0x0001
    PORT_PATTERN: str = r"/dev/ttyACM\d+"
    # 兼容的 USB VID/PID 列表(默认含 19d1:0001; 如需支持更多变体在此追加 (vid, pid) 元组)
    LUAT_VID_PID_LIST: list = field(default_factory=lambda: [(0x19D1, 0x0001)])


# ==================== Air780 设备家族(系列)定义 ====================
# 四个系列底层逻辑与架构完全一致, 业务代码零分支; 仅按系列标注元数据, 便于前端展示与扩展。
AIR780_SERIES: Dict[str, dict] = {
    "Air780E": {
        "chipset": "EC618",
        "network": "LTE Cat.1 bis",
        "form_factor": "LCC + 32pin LGA",
        "usb_vid": 0x19D1, "usb_pid": 0x0001,
        "desc": "基础版, 单卡单待, 通用短信/通话场景",
    },
    "Air780EG": {
        "chipset": "EC618",
        "network": "LTE Cat.1 bis",
        "form_factor": "LCC + 32pin LGA (集成 GNSS)",
        "usb_vid": 0x19D1, "usb_pid": 0x0001,
        "desc": "在 Air780E 基础上集成 GNSS 定位, 短信/通话能力完全一致",
    },
    "Air780EP": {
        "chipset": "EC718",
        "network": "LTE Cat.1 bis",
        "form_factor": "LCC + 56pin LGA",
        "usb_vid": 0x19D1, "usb_pid": 0x0001,
        "desc": "EC718 平台, 更多 GPIO/外设资源, 短信/通话能力完全一致",
    },
    "Air780EH": {
        "chipset": "EC718",
        "network": "LTE Cat.1 bis",
        "form_factor": "LCC + 56pin LGA (集成 GNSS)",
        "usb_vid": 0x19D1, "usb_pid": 0x0001,
        "desc": "EC718 平台 + GNSS 定位, 短信/通话能力完全一致",
    },
}

# 已知 IMEI TAC(前 8 位) -> 系列 映射。
# 真实 TAC 由合宙(AirM2M)分配, 部署时按实际模组补充; 缺失则回退通用 "Air780"。
IMEI_TAC_TO_SERIES: Dict[str, str] = {
    # "XXXXXXXX": "Air780EG",
    # "YYYYYYYY": "Air780EP",
    # "ZZZZZZZZ": "Air780EH",
}

# 系列回退(无法精确识别时的通用标注, 业务逻辑仍完全可用)
DEFAULT_SERIES = "Air780"


def classify_series(model: str = "", imei: str = "") -> str:
    """根据固件上报的 model 或 IMEI TAC 推断设备系列。

    优先级:
      1. 固件显式上报的 model(如 "Air780EG") -> 直接命中已知系列;
      2. IMEI 前 8 位(TAC)查 IMEI_TAC_TO_SERIES;
      3. 都失败 -> 回退通用系列 DEFAULT_SERIES(业务仍可用, 仅标注降级)。
    """
    if model:
        up = model.strip().upper()
        for series in AIR780_SERIES:
            if series.upper() in up:
                return series
        if up.startswith("AIR780"):
            return DEFAULT_SERIES
    if imei and len(imei) >= 8:
        tac = imei[:8]
        if tac in IMEI_TAC_TO_SERIES:
            return IMEI_TAC_TO_SERIES[tac]
    return DEFAULT_SERIES


# ==================== 日志配置 ====================
def setup_logging(config: Config):
    """
    配置日志系统: 按天轮转写入 logs/omnisms.log,
    历史文件自动保留为 logs/omnisms.log.YYYY-MM-DD
    """
    from logging.handlers import TimedRotatingFileHandler
    import os

    log_format = "[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # 确保日志目录存在
    os.makedirs(config.LOG_DIR, exist_ok=True)

    # 文件日志 (按天轮转, 保留 N 天)
    file_handler = TimedRotatingFileHandler(
        filename=config.LOG_FILE,
        when="midnight",
        interval=1,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
        utc=False
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    # 控制台日志
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    # 根日志器: 先清空已有 handler, 避免重复添加
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    return root_logger


logger = logging.getLogger("OmniSMS")

# 全系统时区基准: 底层一律以 UTC 生成时间戳, 前端渲染时再换算为浏览器本地时区。
# 模组(Air780E)无时区概念, 其上报的本地时间按北京时区(UTC+8)解释后统一换算为 UTC 存储。
UTC_TZ = timezone.utc
DEVICE_TZ = timezone(timedelta(hours=8))  # Air780E 模组本地时区: 北京 UTC+8


def utc_now() -> datetime:
    """统一时间戳入口: 返回当前 UTC 时间(带时区信息)。"""
    return datetime.now(UTC_TZ)


def utc_timestamp() -> str:
    """底层统一获取时间戳: UTC ISO 8601 (带 +00:00 时区后缀)。

    所有模块均调用本函数, 存储为无歧义的 UTC 时间戳,
    由前端渲染时换算为用户浏览器本地时区。
    """
    return utc_now().isoformat(timespec="seconds")


def device_local_to_utc_iso(local_str: str) -> str:
    """将模组上报的本地时间字符串(YYYY-MM-DD HH:MM:SS, 北京时区)转换为 UTC ISO 8601。

    模组 os.date 输出为北京本地时间且无时区信息; 此处按 DEVICE_TZ 解释并换算为
    UTC 存储, 避免直接当作浏览器本地时间导致跨时区显示错误。解析失败则回退到当前 UTC。
    """
    if not local_str:
        return utc_timestamp()
    try:
        naive = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S")
        aware = naive.replace(tzinfo=DEVICE_TZ)
        return aware.astimezone(UTC_TZ).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return utc_timestamp()


# ==================== 数据模型 ====================
@dataclass
class DeviceInfo:
    """设备信息数据结构"""
    phone: str = ""         # 本机号码(MSISDN), 业务主键; 缺失时回退 imsi(卡的标识), 再回退 imei
    imei: str = ""
    iccid: str = ""
    at_port: str = ""        # 运行时动态分配的业务端口(VUART_0)，不固定为某个 ttyACM 编号
    log_port: str = ""       # 调试日志端口(DBG)，ttyACM 编号由系统动态分配
    physical_path: str = ""  # USB 物理设备路径，例如 1-1.2
    paired_ports: set = field(default_factory=set)  # 同一物理模组的全部串口
    status: str = "offline"  # online | offline | error
    last_seen: str = ""      # 最后活跃时间(UTC ISO 8601, 带 +00:00 时区后缀)
    rssi: Optional[int] = None  # 最近一次心跳信号强度(dBm)
    rsrp: Optional[int] = None
    rsrq: Optional[int] = None
    snr: Optional[int] = None
    net_status: Optional[int] = None
    imsi: Optional[str] = None   # 国际移动用户标识
    csq: Optional[int] = None    # 信号质量(4G 仅供参考)
    bands: Optional[str] = None  # 当前工作频段列表(JSON 字符串)
    series: str = ""             # 设备系列(由 model/IMEI TAC 推断, 如 Air780EG)
    model: str = ""              # 设备型号(固件上报, 如 Air780EG)
    serial_obj: Optional[serial.Serial] = None  # 串口对象
    write_lock: threading.Lock = field(default_factory=threading.Lock)  # 写入锁


def device_id_of(device: "DeviceInfo") -> str:
    """设备业务标识: 本机号码(MSISDN) 优先, 缺失时回退 IMSI(卡的标识), 再回退 IMEI。"""
    if not device:
        return ""
    return device.phone or device.imsi or device.imei


@dataclass 
class SMSTask:
    """发送短信任务"""
    task_id: str
    phone: str
    text: str
    timestamp: str


# ==================== OmniSMS 核心引擎 ====================
class OmniSMSEngine:
    """
    OmniSMS 主引擎
    负责: 端口发现、设备注册、消息路由、线程管理
    """
    
    @staticmethod
    def _usb_physical_path(port: str) -> Optional[str]:
        """读取 ttyACM 对应的 USB 设备级物理路径。

        例如 1-1.2:1.0 / 1-1.2:1.4 均归属于 USB 设备 1-1.2。
        """
        tty_name = os.path.basename(port)
        if not re.match(r"^ttyACM\d+$", tty_name):
            return None

        sysfs_device = Path("/sys/class/tty") / tty_name / "device"
        try:
            resolved = str(sysfs_device.resolve())
        except OSError:
            return None

        match = re.search(r"/usb\d+/(\d+(?:-\d+)+(?:\.\d+)?)[:/]", resolved)
        if not match:
            match = re.search(r"/(\d+(?:-\d+)+(?:\.\d+)?)[:/]", resolved)
        return match.group(1) if match else None

    @staticmethod
    def _port_index(port: str) -> Optional[int]:
        """获取 ttyACM 数字偏移，用于区分同一模组的接口端口。"""
        match = re.search(r"ttyACM(\\d+)$", os.path.basename(port))
        return int(match.group(1)) if match else None

    def __init__(self, config: Config):
        self.config = config
        
        # {device_id: DeviceInfo} 设备注册表 (device_id = 本机号码 或 回退 IMSI, 再回退 IMEI)
        self.devices: Dict[str, DeviceInfo] = {}
        
        # {port_path: device_id} 端口到设备的映射
        self.port_to_device: Dict[str, str] = {}

        # 手动直连端口集合, 不被扫描清理逻辑移除
        self.manual_ports: set = set()

        # 已探测过的设备端口组: {physical_path: {"ports": frozenset, "registered": bool, "last_probe": float}}
        # 用于确定设备后不再每周期重复扫描其全部端口
        self.known_groups: Dict[str, dict] = {}
        self.observed_ports: frozenset = frozenset()
        
        # 线程控制标志
        self.running = False
        self.scan_thread: Optional[threading.Thread] = None
        # 手动扫描状态: scanning 表示是否正在手动扫描, scan_stop_event 用于提前停止
        self.scanning = False
        self.scan_stop_event = threading.Event()
        # 自动扫描(后台持续)状态: auto_scanning 表示是否正在后台自动扫描,
        # auto_scan_stop 用于随时中断自动扫描(不影响已注册设备)
        self.auto_scanning = False
        self.auto_scan_stop = threading.Event()
        self.auto_scan_thread: Optional[threading.Thread] = None
        self.scan_operation_lock = threading.Lock()
        self.auto_scan_resume_after_manual = False
        self.reader_threads: Dict[str, threading.Thread] = {}
        
        # 待发送任务队列（简化版，生产环境建议用 Queue）
        self.pending_tasks: Dict[str, SMSTask] = {}
        
        # 主锁，保护共享状态
        self.state_lock = threading.RLock()
        
        # 持久化数据库 (由 web.py 注入; 独立运行时可自行创建)
        self.db: Optional[Database] = None
        
        # 业务事件回调 (供 web.py 推送到 WebSocket)
        self.event_callback: Optional[Callable] = None
        
        # 进行中的通话记录 {device_id: call_id}
        self.active_calls: Dict[str, int] = {}
        
        logger.info("OmniSMS Engine initialized")
    
    def start(self):
        """启动引擎并开启后台自动扫描(持续发现并注册设备)"""
        logger.info("=" * 60)
        logger.info("OmniSMS Daemon Starting...")
        logger.info(f"Config: baud={self.config.BAUD_RATE}")
        logger.info("=" * 60)

        self.running = True
        self.start_auto_scan()

        logger.info("OmniSMS Engine started successfully (auto scan enabled)")

    def stop(self):
        """停止引擎"""
        logger.info("Stopping OmniSMS Engine...")
        self.running = False

        # 停止任何进行中的手动扫描与后台自动扫描
        self.scan_stop_event.set()
        self.auto_scan_stop.set()

        # 停止所有读取线程
        with self.state_lock:
            for port, thread in self.reader_threads.items():
                logger.debug(f"Stopping reader thread for {port}")
                thread.join(timeout=2.0)
            self.reader_threads.clear()
            
            # 关闭所有串口连接
            for device in self.devices.values():
                if device.serial_obj and device.serial_obj.is_open:
                    try:
                        device.serial_obj.close()
                        logger.info(f"Closed port: {device.at_port}")
                    except Exception as e:
                        logger.error(f"Error closing port {device.at_port}: {e}")
            
            self.devices.clear()
            self.port_to_device.clear()
        
        if self.scan_thread:
            self.scan_thread.join(timeout=3.0)

        if self.auto_scan_thread:
            self.auto_scan_thread.join(timeout=3.0)
        
        logger.info("OmniSMS Engine stopped")
    
    # ==================== 手动扫描控制 ====================

    def start_manual_scan(self, per_port_timeout: float = 15.0) -> bool:
        """
        启动一次手动扫描。对当前每个候选端口独立探测, 每个端口总计最多等待
        per_port_timeout 秒(可被"停止扫描"提前中断)。
        返回 True 表示已启动, False 表示已有扫描在进行中。
        """
        with self.state_lock:
            if self.scanning:
                logger.info("Manual scan already in progress; ignoring new request")
                return False
            if self.auto_scanning:
                self.auto_scan_stop.set()
                self.auto_scan_resume_after_manual = True
            self.scan_stop_event.clear()
            self.scanning = True

        self.scan_thread = threading.Thread(
            target=self._manual_scan_worker,
            args=(per_port_timeout,),
            name="ManualScanner",
            daemon=True
        )
        self.scan_thread.start()
        logger.info(f"Manual scan started, per_port_timeout={per_port_timeout}s")
        return True

    def stop_manual_scan(self) -> bool:
        """提前停止正在进行的手动扫描。返回 True 表示已发出停止信号。"""
        with self.state_lock:
            if not self.scanning:
                return False
            self.scan_stop_event.set()
        logger.info("Manual scan stop requested")
        return True

    @property
    def is_scanning(self) -> bool:
        return self.scanning

    # ==================== 自动扫描控制 ====================

    def start_auto_scan(self) -> bool:
        """
        启动后台自动扫描: 持续发现并注册设备。
        未扫描到设备时一直运行; 一旦扫描到设备, 再额外运行最多
        AUTO_SCAN_STOP_AFTER_DEVICE_SEC 秒后自动停止。
        已运行时返回 False。不影响已注册设备。
        """
        with self.state_lock:
            if self.auto_scanning or self.scanning:
                logger.info("A scan is already in progress; ignoring auto scan")
                return False
            self.auto_scan_stop.clear()
            self.auto_scanning = True

        self.auto_scan_thread = threading.Thread(
            target=self._auto_scan_worker,
            name="AutoScanner",
            daemon=True
        )
        self.auto_scan_thread.start()
        logger.info("Auto scan started")
        return True

    def stop_auto_scan(self) -> bool:
        """停止后台自动扫描(不影响已注册设备)。返回 True 表示已发出停止信号。"""
        with self.state_lock:
            if not self.auto_scanning:
                return False
            self.auto_scan_stop.set()
        logger.info("Auto scan stop requested")
        return True

    @property
    def is_auto_scanning(self) -> bool:
        return self.auto_scanning

    def _manual_scan_worker(self, per_port_timeout: float):
        """
        手动扫描工作线程: 对当前每个候选端口独立探测, 每个端口最多等待
        per_port_timeout 秒。命中即注册并跳到下一端口; 可被 stop 信号中断。
        """
        try:
            with self.scan_operation_lock:
                self._discover_devices(
                    probe_timeout=per_port_timeout,
                    stop_event=self.scan_stop_event,
                    force_probe=True,
                )
        except Exception as e:
            logger.error(f"Error in manual scan: {e}")
        finally:
            should_resume_auto = False
            with self.state_lock:
                self.scanning = False
                should_resume_auto = self.auto_scan_resume_after_manual
                self.auto_scan_resume_after_manual = False
                auto_thread = self.auto_scan_thread
            if should_resume_auto and auto_thread and auto_thread is not threading.current_thread():
                auto_thread.join(timeout=2.0)
            if should_resume_auto and self.running:
                self.start_auto_scan()
            logger.info("Manual scan finished")
    
    # ==================== 动态端口发现 ====================
    
    def _auto_scan_worker(self):
        """后台自动扫描工作线程。

        行为:
          - 未扫描到任何设备时持续运行, 便于模组后续插入时被发现。
          - 一旦在本轮自动扫描中发现(注册)了设备, 再额外运行最多
            AUTO_SCAN_STOP_AFTER_DEVICE_SEC 秒后自动停止, 以便捕获同模组其它端口/其它模组,
            之后停止后台自动扫描(不影响已注册设备)。配置 <=0 时不自动停止。
        """
        device_found_at = None
        try:
            while self.running and not self.auto_scan_stop.is_set():
                try:
                    before = len(self.devices)
                    with self.scan_operation_lock:
                        topology_changed = self._discover_devices(stop_event=self.auto_scan_stop)
                    # 本轮发现(注册)了新设备 -> 记录首次发现时间, 启动宽限期倒计时
                    if len(self.devices) > before and device_found_at is None:
                        device_found_at = time.time()
                        logger.info("Auto scan detected device; auto-stop in %.0f seconds",
                                    self.config.AUTO_SCAN_STOP_AFTER_DEVICE_SEC)
                except Exception as e:
                    logger.error(f"Error in auto scan: {e}")
                    topology_changed = False

                # 已发现设备且超过宽限期 -> 自动停止(配置 <=0 表示不自动停止)
                grace = self.config.AUTO_SCAN_STOP_AFTER_DEVICE_SEC
                if device_found_at is not None and grace > 0 and \
                        (time.time() - device_found_at) >= grace:
                    logger.info("Auto scan auto-stopped after device discovery grace period")
                    break

                # USB 拓扑变化时立即重新枚举，否则保持常规扫描间隔
                wait_time = 0.0 if topology_changed else self.config.SCAN_INTERVAL_SEC
                if self.auto_scan_stop.wait(wait_time):
                    break
        finally:
            with self.state_lock:
                self.auto_scanning = False
            logger.info("Auto scan finished")

    def _discover_devices(self, probe_timeout: Optional[float] = None,
                          stop_event: Optional[threading.Event] = None,
                          force_probe: bool = False):
        """发现并注册新设备，按 USB 物理路径配对同一模组的串口。

        probe_timeout: 单个端口探测(等待固件事件)的超时秒数。手动扫描时传入
        每端口总预算(如 15s); 后台自动扫描传 None, 使用 BOOT_TIMEOUT 作为总预算。
        stop_event: 用于随时中断扫描(手动扫描传 scan_stop_event, 自动扫描传
        auto_scan_stop); 提供时一旦置位即中止本轮发现。
        force_probe: 手动扫描为 True, 忽略近期失败端口组的重探测节流。
        """
        current_ports = set()

        # 扫描 Air780 家族(含 Air780E/EG/EP/EH)的 USB VID/PID
        # 默认 19d1:0001; 兼容列表见 Config.LUAT_VID_PID_LIST, 并始终包含历史 LUAT_VID/PID
        vid_pid_set = set(self.config.LUAT_VID_PID_LIST)
        vid_pid_set.add((self.config.LUAT_VID, self.config.LUAT_PID))
        ports = serial.tools.list_ports.comports()
        air780e_ports = [
            p for p in ports
            if (p.vid, p.pid) in vid_pid_set
        ]

        # 按 USB 物理设备分组；无法解析物理路径的端口独立成组。
        port_groups = {}
        for port_info in air780e_ports:
            port = port_info.device
            physical_path = self._usb_physical_path(port) or f"port:{port}"
            port_groups.setdefault(physical_path, []).append(port)

        logger.debug("Air780 family port groups: %s", port_groups)
        current_topology = frozenset(air780e_ports[i].device for i in range(len(air780e_ports)))
        topology_changed = current_topology != self.observed_ports
        self.observed_ports = current_topology
        if topology_changed:
            logger.info("Air780 family port topology changed; scheduling immediate re-probe")

        now = time.time()

        # 组间按"每组最小 ttyACM 序号"升序遍历, 确保手动扫描从第一个端口(最小 ttyACMx)开始
        def _group_min_index(item):
            _, ports = item
            idxs = [self._port_index(p) for p in ports]
            idxs = [i for i in idxs if i is not None]
            return min(idxs) if idxs else 0

        ordered_groups = sorted(port_groups.items(), key=_group_min_index)

        for physical_path, group_ports in ordered_groups:
            # 扫描期间可被停止信号提前中断
            if stop_event is not None and stop_event.is_set():
                logger.info("Scan stop requested; aborting discovery")
                break
            group_ports = sorted(
                set(group_ports),
                key=lambda port: (self._port_index(port) is None,
                                  self._port_index(port) or 0, port)
            )
            current_ports.update(group_ports)

            # 已确定过的设备端口组: 已注册组只探测新增端口，失败组按策略节流
            known = self.known_groups.get(physical_path)
            probe_ports = list(group_ports)
            if known and known["registered"]:
                if force_probe:
                    # 手动扫描: 强制从第一个端口重新探测已注册组。
                    # 先拆除该物理组下的旧设备(关闭串口 + 终止读取线程 + 清理映射),
                    # 避免与现有读取线程争用同一 at_port, 随后按首个端口重新握手注册。
                    logger.info(f"Manual scan: force re-probe registered group {physical_path}; "
                                f"tearing down existing device(s) first")
                    with self.state_lock:
                        for dev in list(self.devices.values()):
                            if dev.physical_path == physical_path:
                                self.remove_device(device_id_of(dev))
                    self.known_groups.pop(physical_path, None)
                    known = None
                    probe_ports = list(group_ports)
                else:
                    registered_ports = set()
                    with self.state_lock:
                        for device in self.devices.values():
                            if device.physical_path == physical_path:
                                registered_ports.update(device.paired_ports)
                    probe_ports = sorted(set(group_ports) - registered_ports)
                    if not probe_ports:
                        logger.debug(f"Device group {physical_path} already registered; skipping scan")
                        continue
            elif (known and known["ports"] == frozenset(group_ports) and
                  not force_probe and
                  now - known["last_probe"] < self.config.RESCAN_KNOWN_GROUP_SEC):
                logger.debug(f"Device group {physical_path} determined but not registered; "
                             f"throttled re-probe")
                continue

            # 同一物理设备的多个 CDC 接口中, 只有 VUART_0 运行 OmniSMS 串口协议。
            # 逐个端口尝试 identify 握手, 命中首个回 boot 事件的端口作为 at_port。
            for candidate in probe_ports:
                if stop_event is not None and stop_event.is_set():
                    break
                if not any(p.device == candidate for p in serial.tools.list_ports.comports()):
                    logger.debug(f"Port {candidate} disappeared before probe")
                    continue
                self._try_register_device(
                    candidate, physical_path, set(group_ports),
                    probe_timeout=probe_timeout, stop_event=stop_event,
                )
                if candidate in self.port_to_device:
                    break

            # 记录该端口组已探测: 注册成功则永久跳过; 否则按低速间隔重试
            registered = any(p in self.port_to_device for p in group_ports)
            self.known_groups[physical_path] = {
                "ports": frozenset(group_ports),
                "registered": registered,
                "last_probe": now,
            }

        # 清理已消失的端口；同组任一端口消失都视为设备断开。
        with self.state_lock:
            disconnected = []
            for port, dev_id in list(self.port_to_device.items()):
                if port in self.manual_ports:
                    continue
                device = self.devices.get(dev_id)
                if port not in current_ports and device and port == device.at_port:
                    disconnected.append((port, dev_id))

            for port, imei in disconnected:
                self._handle_disconnect(port, imei)
                # 设备断开后从已知组移除, 以便其重连(端口变化)后重新探测
                self.known_groups.pop(self._usb_physical_path(port) or "", None)

        # 清理已彻底消失(不在当前端口组中)的已知组, 避免残留
        for pp in list(self.known_groups.keys()):
            if pp not in port_groups:
                self.known_groups.pop(pp, None)

        return topology_changed

    def _try_register_device(self, port: str, physical_path: str = "",
                             paired_ports=None, probe_timeout: Optional[float] = None,
                             stop_event: Optional[threading.Event] = None):
        """在单个端口的总预算内周期握手并注册设备。"""
        paired_ports = set(paired_ports or {port})
        total_timeout = probe_timeout if probe_timeout is not None else self.config.BOOT_TIMEOUT
        stop = stop_event if stop_event is not None else self.scan_stop_event
        deadline = time.monotonic() + total_timeout
        logger.info(f"Trying to identify new device on {port} (timeout={total_timeout}s)...")

        try:
            ser = serial.Serial(
                port=port,
                baudrate=self.config.BAUD_RATE,
                timeout=self.config.SERIAL_TIMEOUT
            )

            self._send_identify(ser, port)
            fw_event = self._wait_for_firmware_event(
                ser, deadline, stop_event=stop,
                identify_callback=lambda: self._send_identify(ser, port),
                identify_interval=self.config.IDENTIFY_INTERVAL_SEC,
            )

            if not fw_event:
                logger.warning(f"No firmware event received from {port}, skipping")
                ser.close()
                return

            event = fw_event.get("event")
            imei = fw_event.get("imei", "")
            iccid = fw_event.get("iccid", "")
            imsi = fw_event.get("imsi", "") or ""
            number = fw_event.get("number", "") or ""
            model = fw_event.get("model", "") or ""

            if event in self.config.FIRMWARE_EVENTS:
                if imei and len(imei) >= 15:
                    self._register_device(
                        imei, iccid, imsi, number, ser, port,
                        physical_path=physical_path,
                        paired_ports=paired_ports,
                        model=model,
                    )
                    return

                remaining = max(0.0, deadline - time.monotonic())
                if remaining > 0 and not stop.is_set():
                    logger.info(
                        f"{port} is a firmware data port (event={event}); "
                        f"re-sending identify within remaining {remaining:.1f}s"
                    )
                    self._send_identify(ser, port)
                    followup = self._wait_for_firmware_event(
                        ser, deadline, stop_event=stop,
                        identify_callback=lambda: self._send_identify(ser, port),
                        identify_interval=self.config.IDENTIFY_INTERVAL_SEC,
                    )
                    if followup and followup.get("imei") and len(followup.get("imei")) >= 15:
                        self._register_device(
                            followup.get("imei"), followup.get("iccid", ""),
                            followup.get("imsi", "") or "",
                            followup.get("number", "") or "",
                            ser, port, physical_path=physical_path,
                            paired_ports=paired_ports,
                            model=followup.get("model", "") or model,
                        )
                        return

                logger.warning(
                    f"{port} is a firmware data port but no valid IMEI available; skipping"
                )
                ser.close()
                return

            logger.debug(f"{port} returned non-firmware data (event={event}); skipping")
            ser.close()
            return

            # 3) 非固件事件(Lua REPL 报错 / AT 回显等): 不是目标口
            logger.debug(f"{port} returned non-firmware data (event={event}); skipping")
            ser.close()

        except serial.SerialException as e:
            logger.error(f"Cannot open port {port}: {e}")
        except Exception as e:
            logger.error(f"Error registering device at {port}: {e}")

    def _send_identify(self, ser: serial.Serial, port: str):
        """向设备发送 identify 握手(尽力而为, 失败仅记录)。"""
        try:
            handshake = {"action": "identify"}
            ser.write((json.dumps(handshake) + "\n").encode('utf-8'))
            ser.flush()
            logger.debug(f"Sent identify handshake to {port}")
        except Exception as e:
            logger.debug(f"Failed to send identify to {port}: {e}")

    def _wait_for_firmware_event(
        self, ser: serial.Serial, deadline: float,
        stop_event: Optional[threading.Event] = None,
        identify_callback: Optional[Callable] = None,
        identify_interval: float = 0.0,
    ) -> Optional[dict]:
        """在绝对截止时间前等待固件事件，并按间隔重发握手。"""
        next_identify = time.monotonic() + identify_interval if identify_interval > 0 else None
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                logger.debug(f"{ser.name} firmware-event wait interrupted by stop signal")
                return None
            if next_identify is not None and time.monotonic() >= next_identify:
                identify_callback()
                next_identify = time.monotonic() + identify_interval
            try:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    time.sleep(0.1)
                    continue

                logger.debug(f"Raw data during discovery: {line[:80]}")

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # 非 JSON: 可能是 AT 回显 / Lua REPL 报错 / 二进制 trace。
                    # 仅在明确是 Lua REPL 报错时快速判定非固件口; 其余情况继续等待,
                    # 避免漏掉"先有噪声、后有 JSON"的固件数据口。
                    if "repl" in line.lower() or "unexpected symbol" in line.lower():
                        logger.debug(f"Lua REPL error from {ser.name}; not a firmware port")
                        return None
                    continue

                if isinstance(msg, dict) and msg.get("event") in self.config.FIRMWARE_EVENTS:
                    return msg

            except serial.SerialException as e:
                logger.error(f"Serial error during discovery wait: {e}")
                break

        return None

    def _wait_for_boot_event(self, ser: serial.Serial, timeout: float,
                             stop_event: Optional[threading.Event] = None) -> Optional[dict]:
        """等待设备发送 boot 事件(含 imei/iccid)。
        若提供 stop_event 且被置位, 则提前返回 None(用于响应"停止扫描")。"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if stop_event is not None and stop_event.is_set():
                logger.debug(f"{ser.name} boot-event wait interrupted by stop signal")
                return None
            try:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    time.sleep(0.1)
                    continue

                logger.debug(f"Raw data during discovery: {line[:80]}")

                try:
                    msg = json.loads(line)
                    if isinstance(msg, dict) and msg.get("event") == "boot":
                        return msg
                except json.JSONDecodeError:
                    continue

            except serial.SerialException as e:
                logger.error(f"Serial error during boot wait: {e}")
                break

        return None
    
    def _register_device(self, imei: str, iccid: str, imsi: str, phone: str, ser: serial.Serial,
                         port: str, physical_path: str = "", paired_ports=None, model: str = ""):
        """注册新设备并启动 AT 端口读取线程。device_id = 本机号码 或 回退 IMSI(卡的标识) 或 IMEI。"""
        now = utc_timestamp()
        paired_ports = set(paired_ports or {port})
        log_ports = sorted(paired_ports - {port})
        device_id = phone or imsi or imei
        # 系列识别: 由固件上报 model 或 IMEI TAC 推断, 用于前端标注(业务零分支)
        series = classify_series(model, imei)

        device = DeviceInfo(
            phone=phone,
            imei=imei,
            iccid=iccid,
            imsi=imsi,
            at_port=port,
            log_port=log_ports[0] if log_ports else "",
            physical_path=physical_path or self._usb_physical_path(port) or "",
            paired_ports=paired_ports,
            series=series,
            model=model,
            status="online",
            last_seen=now,
            serial_obj=ser
        )
        
        with self.state_lock:
            # 如果同一 device_id 已存在，更新
            if device_id in self.devices:
                old_device = self.devices[device_id]
                logger.info(f"Device reconnected: {device_id}")
                if old_device.serial_obj and old_device.serial_obj.is_open:
                    try:
                        old_device.serial_obj.close()
                    except:
                        pass
            
            self.devices[device_id] = device
            for paired_port in paired_ports:
                self.port_to_device[paired_port] = device_id
        
        # 启动读取线程
        reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(device,),
            name=f"Reader-{port.split('/')[-1]}",
            daemon=True
        )
        reader_thread.start()
        self.reader_threads[port] = reader_thread
        
        # 持久化设备信息
        if self.db:
            self.db.upsert_device(
                device_id=device_id, phone=phone, imei=imei, iccid=iccid,
                at_port=port,
                log_port=log_ports[0] if log_ports else "",
                status="online", last_seen=now,
                series=series, model=model
            )
        
        logger.info(f"✓ Device registered: ID={device_id}, IMEI={imei}, Port={port}")

        # 通知上层 (web.py) 设备上线, 用于 WebSocket 实时刷新设备列表
        self._notify_event("device_online", device_id, {"status": "online", "port": port})

    def _migrate_device_id(self, old_id: str, new_id: str, device: DeviceInfo):
        """设备标识迁移: 号码/IMSI 到位时从旧标识(IMEI 兜底)迁移到更优标识。"""
        with self.state_lock:
            if old_id in self.devices:
                del self.devices[old_id]
            self.devices[new_id] = device
            for paired_port in getattr(device, "paired_ports", set()) or set():
                self.port_to_device[paired_port] = new_id
            if old_id in self.active_calls:
                self.active_calls[new_id] = self.active_calls.pop(old_id)
        # 旧 devices 行(以 IMEI 兜底)删除, 避免重复; 刚启动阶段历史短信/通话极少
        if self.db:
            self.db.delete_device(old_id)
            self.db.upsert_device(
                device_id=new_id, phone=device.phone, imei=device.imei,
                iccid=device.iccid, at_port=device.at_port, log_port=device.log_port,
                status=device.status, last_seen=device.last_seen,
                rssi=device.rssi, rsrp=device.rsrp, rsrq=device.rsrq,
                snr=device.snr, net_status=device.net_status,
                imsi=device.imsi, csq=device.csq, bands=device.bands,
                series=device.series, model=device.model,
            )
        logger.info(f"Device identity migrated: {old_id} -> {new_id}")

    def _handle_disconnect(self, port: str, device_id: str):
        """处理设备断开"""
        logger.warning(f"✗ Device disconnected: {port} (ID={device_id})")
        
        # 更新数据库状态
        if self.db:
            self.db.update_device_status(device_id, "offline")
        
        with self.state_lock:
            if device_id in self.devices:
                self.devices[device_id].status = "offline"
            
            if port in self.port_to_device:
                del self.port_to_device[port]
            
            if port in self.reader_threads:
                del self.reader_threads[port]
    
    def remove_device(self, device_id: str):
        """从引擎内存中彻底移除设备（关闭串口、清理端口映射与读取线程）。"""
        with self.state_lock:
            device = self.devices.get(device_id)
            if not device:
                logger.debug(f"remove_device: {device_id} not in engine, skipping")
                return

            # 关闭串口, 读取线程会在检测到串口关闭后自行退出
            if device.serial_obj and device.serial_obj.is_open:
                try:
                    device.serial_obj.close()
                except Exception as e:
                    logger.debug(f"Error closing port for {device_id}: {e}")

            # 清理端口映射与读取线程记录
            for port in list(getattr(device, "paired_ports", set()) or set()):
                self.port_to_device.pop(port, None)
                self.reader_threads.pop(port, None)
            self.devices.pop(device_id, None)

            logger.info(f"✗ Device removed from engine: ID={device_id}")

    # ==================== 串口读取循环 ====================
    
    def _reader_loop(self, device: DeviceInfo):
        """设备消息读取循环"""
        logger.info(f"Starting reader loop for {device.at_port or device.log_port}")
        
        while self.running:
            if not device.serial_obj or not device.serial_obj.is_open:
                logger.error(f"Serial port closed unexpectedly: {device.at_port}")
                break
            
            try:
                line = device.serial_obj.readline().decode('utf-8', errors='ignore').strip()
                
                if line:
                    self._handle_incoming_message(device, line)
                    
            except serial.SerialException as e:
                logger.error(f"SerialException on {device.at_port}: {e}")
                self._handle_disconnect(device.at_port or device.log_port, device_id_of(device))
                break
            except Exception as e:
                logger.error(f"Unexpected error reading from {device.at_port}: {e}")
                time.sleep(0.5)
        
        logger.info(f"Reader loop ended for {device.at_port or device.log_port}")
    
    # ==================== 消息处理 ====================
    
    def _handle_incoming_message(self, device: DeviceInfo, raw_line: str):
        """处理从设备接收到的消息"""
        try:
            msg = json.loads(raw_line)
            
            if not isinstance(msg, dict):
                logger.warning(f"Not a JSON object: {raw_line[:50]}")
                return
            
            event_type = msg.get("event", "")
            action_type = msg.get("action", "")
            
            if event_type:
                # 上行事件 (LuatOS -> Python)
                self._handle_upstream_event(device, event_type, msg)
                if event_type in self.config.FIRMWARE_EVENTS:
                    device.last_seen = utc_timestamp()
            elif action_type:
                # 下行响应 (不应该出现，仅作容错)
                logger.warning(f"Unexpected action response from device: {action_type}")
            else:
                logger.warning(f"Unknown message format: {raw_line[:50]}")
                
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON line (debug log?): {raw_line[:100]}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    def _handle_upstream_event(self, device: DeviceInfo, event_type: str, msg: dict):
        """处理上行事件"""
        dev_id = device_id_of(device)
        logger.info(f"[{event_type}] From {dev_id}: {json.dumps(msg, ensure_ascii=False)[:100]}")

        # 刷新设备系列标注: 固件在 boot/keepalive/netinfo 中携带 model 时更新(业务零分支)
        reported_model = msg.get("model")
        if reported_model:
            device.model = reported_model
            device.series = classify_series(reported_model, device.imei)

        if event_type == "log":
            # LuatOS 通过业务通道转发的运行日志
            level = msg.get("level", "info")
            tag = msg.get("tag", "LuatOS")
            log_msg = msg.get("msg", "")
            log_func = {
                "debug": logger.debug,
                "info": logger.info,
                "warn": logger.warning,
                "warning": logger.warning,
                "error": logger.error,
                "fatal": logger.critical,
            }.get(level, logger.info)
            log_func(f"[LuatOS:{tag}] {log_msg}")

        elif event_type in {"boot", "keepalive"}:
            # 启动/心跳统一更新网络诊断字段
            was_offline = device.status != "online"
            device.status = "online"
            for field_name in ("rssi", "rsrp", "rsrq", "snr", "net_status"):
                value = msg.get(field_name)
                if isinstance(value, (int, float)):
                    setattr(device, field_name, int(value))
            if self.db:
                self.db.update_device_status(
                    dev_id, "online", last_seen=device.last_seen,
                    phone=device.phone, imei=device.imei, imsi=device.imsi,
                    rssi=device.rssi, rsrp=device.rsrp, rsrq=device.rsrq,
                    snr=device.snr, net_status=device.net_status,
                    series=device.series, model=device.model,
                )
            if was_offline:
                self._notify_event("device_online", dev_id, {
                    "status": "online",
                    "rssi": device.rssi,
                    "rsrp": device.rsrp,
                    "rsrq": device.rsrq,
                    "snr": device.snr,
                    "net_status": device.net_status,
                })


        elif event_type == "netinfo":
            # 网络诊断信息(含 IMSI / CSQ / 频段 / 本机号码 等)
            new_imsi = msg.get("imsi")
            new_csq = msg.get("csq")
            new_bands = msg.get("bands")
            new_number = msg.get("number") or ""
            if isinstance(new_imsi, str):
                device.imsi = new_imsi
            if isinstance(new_csq, (int, float)):
                device.csq = int(new_csq)
            if isinstance(new_bands, list):
                device.bands = json.dumps(new_bands, ensure_ascii=False)
            if isinstance(new_number, str) and new_number:
                device.phone = new_number
            # 同时刷新通用信号字段
            for field_name in ("rssi", "rsrp", "rsrq", "snr", "net_status"):
                value = msg.get(field_name)
                if isinstance(value, (int, float)):
                    setattr(device, field_name, int(value))
            # 本机号码到位而此前以 IMEI 兜底注册 -> 迁移设备标识到号码
            new_dev_id = device_id_of(device)
            if new_dev_id != dev_id:
                self._migrate_device_id(dev_id, new_dev_id, device)
                dev_id = new_dev_id
            if self.db:
                self.db.update_device_status(
                    dev_id, device.status, last_seen=device.last_seen,
                    phone=device.phone, imei=device.imei,
                    rssi=device.rssi, rsrp=device.rsrp, rsrq=device.rsrq,
                    snr=device.snr, net_status=device.net_status,
                    imsi=device.imsi, csq=device.csq, bands=device.bands,
                    series=device.series, model=device.model,
                )


        elif event_type == "sms_received":
            # 收到新短信
            phone = msg.get("phone", "")
            text = msg.get("text", "")
            raw_time = msg.get("time") or ""
            sms_time = device_local_to_utc_iso(raw_time) if raw_time else utc_timestamp()
            
            logger.info(f"SMS Received from {phone}: {text[:50]}...")
            if self.db:
                self.db.add_sms(dev_id, phone, text, "in", "received", None, sms_time)
            self._notify_event("sms_received", dev_id, {
                "phone": phone, "text": text, "time": sms_time
            })
            
        elif event_type == "sms_sent_result":
            # 发送短信结果
            task_id = msg.get("id", "")
            status = msg.get("status", "fail")
            error_code = msg.get("error_code")
            api_return = msg.get("api_return")
            net_status = msg.get("net_status")
            rssi = msg.get("rssi")
            
            logger.info(
                f"SMS Send Result: task={task_id}, status={status}, "
                f"error_code={error_code}, api_return={api_return}, "
                f"net_status={net_status}, rssi={rssi}"
            )
            
            # accepted 仅表示短信协议栈接受请求，不能标记为最终 sent。
            if self.db and task_id and status == "fail":
                self.db.update_sms_status(task_id, "failed")
            self._notify_event("sms_sent_result", dev_id, {
                "id": task_id,
                "status": status,
                "error_code": error_code,
                "reason": msg.get("reason"),
                "text_bytes": msg.get("text_bytes"),
                "api_return": api_return,
                "net_status": net_status,
                "rssi": rssi,
            })
            
            # 从待发队列移除
            if task_id in self.pending_tasks:
                del self.pending_tasks[task_id]
            
        elif event_type == "call_incoming":
            # 来电通知
            phone = msg.get("phone", "")
            logger.info(f"Incoming Call from {phone}")
            if self.db:
                call_id = self.db.add_call(dev_id, phone, "in", "ringing")
                self.active_calls[dev_id] = call_id
            self._notify_event("call_incoming", dev_id, {"phone": phone})
            
        elif event_type == "call_disconnected":
            # 挂断/通话结束
            reason = msg.get("reason", "unknown")
            phone = msg.get("phone", "unknown")
            logger.info(f"Call Disconnected: reason={reason}")
            
            call_id = self.active_calls.get(dev_id)
            now = utc_timestamp()
            if self.db and call_id:
                self.db.update_call(call_id, status="disconnected", end_time=now, duration=0)
                self.active_calls.pop(dev_id, None)
            self._notify_event("call_disconnected", dev_id, {
                "phone": phone, "reason": reason
            })
            
        else:
            logger.warning(f"Unknown upstream event: {event_type}")
    
    def _notify_event(self, event_type: str, device_id: str, data: dict):
        """通知上层 (web.py) 有新业务事件, 用于 WebSocket 推送"""
        if self.event_callback:
            try:
                self.event_callback(event_type, device_id, data)
            except Exception as e:
                logger.debug(f"Event callback error: {e}")
    
    # ==================== 下行命令接口 ====================
    
    def send_sms(self, device_id: str, phone: str, text: str) -> str:
        """
        发送短信命令
        @return: 任务ID
        """
        import uuid
        
        task_id = str(uuid.uuid4())
        
        command = {
            "action": "send_sms",
            "id": task_id,
            "phone": phone,
            "text": text
        }
        
        success = self._send_command(device_id, command)
        
        if success:
            # 记录待确认任务
            self.pending_tasks[task_id] = SMSTask(
                task_id=task_id,
                phone=phone,
                text=text,
                timestamp=utc_timestamp()
            )
            # 持久化发送记录
            if self.db:
                self.db.add_sms(device_id, phone, text, "out", "pending", task_id,
                                utc_timestamp())
            logger.info(f"SMS send command dispatched: task={task_id}, to={phone}")
        else:
            logger.error(f"Failed to send SMS command to {device_id}")
        
        return task_id
    
    def make_call(self, device_id: str, phone: str) -> bool:
        """拨打电话"""
        command = {
            "action": "dial",
            "id": str(int(time.time())),
            "phone": phone
        }
        
        success = self._send_command(device_id, command)
        if success and self.db:
            call_id = self.db.add_call(device_id, phone, "out", "dialing")
            self.active_calls[device_id] = call_id
        return success
    
    def hangup_call(self, device_id: str) -> bool:
        """挂断电话"""
        command = {"action": "hangup"}
        
        return self._send_command(device_id, command)
    
    def _send_command(self, device_id: str, command: dict) -> bool:
        """
        发送下行命令到指定设备
        使用线程安全写入
        """
        with self.state_lock:
            device = self.devices.get(device_id)
            
            if not device or not device.serial_obj:
                logger.error(f"Device not found or offline: {device_id}")
                return False
            
            if not device.serial_obj.is_open:
                logger.error(f"Serial port not open for {device_id}")
                return False
            
            try:
                # 加锁写入，确保原子性
                with device.write_lock:
                    cmd_json = json.dumps(command, ensure_ascii=False) + "\n"
                    written = device.serial_obj.write(cmd_json.encode('utf-8'))
                    device.serial_obj.flush()
                    
                    logger.debug(f"Sent to {device_id}: {cmd_json.strip()}")
                    return written > 0
                    
            except serial.SerialException as e:
                logger.error(f"Serial write error to {device_id}: {e}")
                device.status = "error"
                return False
            except Exception as e:
                logger.error(f"Error sending command to {device_id}: {e}")
                return False
    
    # ==================== 状态查询接口 ====================
    
    def get_all_devices(self) -> Dict[str, DeviceInfo]:
        """获取所有已注册设备"""
        with self.state_lock:
            return dict(self.devices)
    
    def get_device(self, device_id: str) -> Optional[DeviceInfo]:
        """通过 device_id(本机号码/IMSI/IMEI) 获取设备; 兼容按真实 imei 或 imsi 查找。"""
        with self.state_lock:
            dev = self.devices.get(device_id)
            if dev:
                return dev
            for d in self.devices.values():
                if d.imei == device_id or d.imsi == device_id:
                    return d
            return None


# ==================== 信号处理与优雅退出 ====================

def signal_handler(signum, frame):
    """处理中断信号"""
    logger.info(f"\nReceived signal {signum}, shutting down...")
    global engine
    if engine:
        engine.stop()
    sys.exit(0)


