# OmniSMS

基于 **FastAPI + LuatOS(Air780 家族)** 的多设备短信/通话管理系统，统一兼容 **Air780E / Air780EG / Air780EP / Air780EH**（系列识别规则与特性差异详见第十节）。当前版本 **v3.0.0**。

一个进程同时承载 **Web 管理界面** 与 **设备守护引擎**，提供设备注册、短信收发、通话控制与实时日志能力。支持两种设备接入方式：

- **本地直连（pyserial）**：宿主进程通过 USB 串口（`/dev/ttyACM*`）直接与真实 Air780 模组通信。
- **WebSerial 桥接**：浏览器 WebSerial API 经 `/ws/webserial` 将 USB 数据桥接到后端引擎（详见第六节）。

两种接入方式**自由切换、二选一**；仅当检测到 **Docker 环境**时，自动禁用 pySerial，只能使用 WebSerial 桥接。

---

## 一、整体协同架构

```mermaid
flowchart TB
    Browser["浏览器 (Web 前端)<br/>index.html + app.js + style.css"]

    Browser -->|"REST API (HTTP)"| Web
    Browser -->|"WebSocket /ws/log"| Web
    Browser -->|"WebSocket /ws/webserial (WebSerial 桥接)"| Web

    subgraph WebLayer["web.py — 唯一程序入口 (FastAPI + uvicorn)"]
        Web["· REST 路由<br/>· 实时事件广播<br/>· 日志读取/推送<br/>· WebSerial 桥接<br/>· init_engine() 启动编排"]
    end

    Web -->|"import / 调用"| Engine

    subgraph Backend["后端引擎层"]
        Engine["omnisms.py<br/>OmniSMSEngine<br/>· 端口发现/注册<br/>· 消息路由/线程<br/>· 下行命令下发"]
    end

    Engine -->|"serial (115200)"| Port[("串口 /dev/ttyACM*")]
    Port -->|"UART (JSON 行协议)"| Device
    Browser -.->|"WebSerial API"| Device
    Device["Air780 家族模组<br/>(Air780E/EG/EP/EH)<br/>LuatOS 固件 (Luatos/*.lua)<br/>main / sms_handler / call_handler"]

    DB[("database.py<br/>SQLite: omnisms.db")]
    Engine -.->|"读写"| DB
    Web -.->|"读写"| DB
```

### 协同链路说明

1. **唯一入口 `web.py`**：启动后调用 `init_engine()` 拉起 `OmniSMSEngine`（守护引擎），随后 `uvicorn.run()` 提供 Web 服务。
2. **引擎 `omnisms.py`**：周期性扫描真实 USB 串口（`/dev/ttyACM*`）；对每个新端口发送 `identify` 握手并等待 `boot` 事件完成设备注册，随后启动读取线程解析设备上行消息。VUART_0 对应的 `ttyACM` 编号不固定，由 daemon 自动按 VID/PID + 握手发现，部署时不要手动写死端口路径。
3. **设备端**：Air780 家族（Air780E/EG/EP/EH）模组运行同一份 `Luatos/` 固件（按硬件设置 `DEVICE_MODEL` 即可区分系列）。
4. **持久化 `database.py`**：引擎与 Web 共享同一个 SQLite 库，保存设备、短信、通话记录。
6. **前端**：通过 REST API 拉取数据，通过 `WebSocket /ws/log` 接收引擎业务事件与日志的实时推送。

---

## 二、目录结构

```
OmniSMS/
├── web.py              # 唯一入口：FastAPI Web 服务 + 启动编排（拉起引擎）+ WebSerial 桥接
├── omnisms.py          # 核心守护引擎 OmniSMSEngine（端口发现、消息路由、下行命令、WebSerial 虚拟端口）
├── database.py         # SQLite 持久化层（线程安全）
├── requirements.txt    # Python 依赖
├── omnisms.sh          # 管理脚本：虚拟环境/守护进程/源码更新
├── omnisms.service     # systemd 服务模板（开机自启）
├── omnisms.db          # SQLite 数据库文件（运行时生成）
├── logs/               # 按天轮转的日志文件
├── static/
│   ├── app.js          # 前端逻辑：设备/短信/通话/日志 + WebSocket
│   └── style.css       # 前端样式
├── templates/
│   └── index.html      # 单页前端模板（Tailwind CDN）
└── Luatos/             # Air780 家族固件（部署到模组，非宿主进程；四份 .lua 通用于各系列）
    ├── main.lua        # 入口：串口通信层(直接收发无缓冲)、网络信息模块(采集/周期上报)、action 分发、boot 事件、30s 心跳、60s netinfo、指示灯联动
    ├── sms_handler.lua # 短信接收回调上报 / send_sms 下行 / sms_sent_result 结果上报(含长短信分片)
    ├── call_handler.lua# CC_IND 来电/挂断事件上报、dial/hangup 下行执行
    └── util_netled.lua # 网络状态指示灯模块：开机呼吸灯、在线慢闪、等待连接快闪、收发短信/来电活动快闪
```

---

## 三、各模块职责

### 1. `web.py`（唯一程序入口）
- 提供所有 REST API 与 `WebSocket /ws/log`、`/ws/webserial`。
- `init_engine()`：构造 `Config` 与 `Database`，创建 `OmniSMSEngine` 并 `start()`；按 Docker 环境/环境变量决定是否禁用 pySerial。
- `broadcast_engine_event()`：引擎业务事件回调，经 `asyncio.call_soon_threadsafe` 安全地推送到 WebSocket。
- `WebSerialBridge`：管理浏览器 WebSerial 连接与后端引擎的桥接（注册/数据转发/命令下发）。
- `WebLogHandler` + `LogFileReader`：将日志写入内存缓存、推送前端，并从 `logs/` 目录读取历史日志（按天轮转）。
- 命令行参数：`--host`、`--port`、`--ssl-cert`、`--ssl-key`。

### 2. `omnisms.py`（守护引擎）
- `OmniSMSEngine`：
  - `_auto_scan_worker` / `_discover_devices`：后台持续按 VID/PID 列表（默认 `19d1:0001`，覆盖 Air780E/EG/EP/EH）扫描真实 USB 串口；`start_auto_scan()` / `stop_auto_scan()` 控制启停。设备系列由 `classify_series()` 依据固件 `model` 或 IMEI TAC 推断，业务零分支。
  - `_try_register_device`：发送 `identify` 握手 → 等待 `boot` 事件（超时 5s）→ 注册设备并启动读取线程。
  - `_reader_loop` / `_handle_incoming_message`：按行解析 JSON，分发上行事件。
  - 下行接口：`send_sms()`（成功返回 `task_id`，失败返回 `None`）、`make_call()`（成功返回 `task_id`，失败返回 `None`）、`hangup_call()`，经 `_send_command()` 线程安全写入串口；同时支持 pyserial 直连与 WebSerial 虚拟端口两种连接类型。
  - 短信终态收敛：`sms_sent_result` 的 `accepted` 只是中间态（协议栈已接受、已提交网络），固件不提供运营商投递回执，因此由看门狗 `_check_pending_sms_tasks()` 在 `SMS_ACCEPTED_TIMEOUT_SEC`（默认 120s）后收敛为 `sent`；命令下发后完全无响应则在 `SMS_PENDING_TIMEOUT_SEC`（默认 300s）后收敛为 `failed`。每条外发短信最终都会落到 `sent` / `failed`，不会永久停留在 `pending`。
  - 设备标识迁移：`_migrate_device_id()` 在号码/IMSI 到位升级 `device_id` 时，会同步调用 `Database.migrate_device_records()` 搬迁历史短信与通话记录，避免旧记录因 `device_id` 失配变成孤儿数据。
  - `WebSerialVirtualPort`：WebSerial 桥接模式下模拟串口对象，供引擎读取线程复用同一套消息处理逻辑。
  - 通话状态：`active_calls` 记录 `(call_id, start_time, direction)`，挂断时计算真实通话时长并回传方向。
- `event_callback`：供 Web 层推送实时事件到前端。

### 3. `database.py`（持久化）
- 线程安全 SQLite 封装（`_lock` 串行化 + 每次操作独立连接）。
- 三张表：`devices`、`sms_messages`、`call_records`（详见第五节）。

### 5. `Luatos/*.lua`（设备端固件）
- `main.lua`：入口与模块整合。串口通信层（直接收发、无自定义缓冲区/发送队列，异步发送用 `sys.taskInit`，`uart.VUART_0` @115200）；网络信息模块（采集 IMEI/IMSI/ICCID/本机号码/CSQ/RSSI/RSRQ/RSRP/SNR/频段/`model`，按 `event=netinfo` 上报并响应 `get_netinfo` 动作）；注册 action 分发器（`send_sms`→`sms_handler`、`dial`/`hangup`→`call_handler`、`get_netinfo`→本地处理、`identify`→重发 `boot` 并点亮在线灯）；等待 SIM 注册后发送 `boot`，启动 30s `keepalive` + 60s `netinfo` 统一监控任务；并联动 `util_netled` 指示灯。
- `sms_handler.lua`：注册短信接收回调（`sms_received` 上行，长短信由 `autoLong` 自动合并）、处理 `send_sms` 下行（超 140 字节走 `sms.sendLong`），回报 `sms_sent_result`（`accepted`/`fail` 及 `error_code`/`reason`/`api_ok` 等）；收发短信时触发 `util_netled.active()` 活动快闪。
- `call_handler.lua`：订阅 `CC_IND`（`INCOMINGCALL`/`CONNECTED`/`DISCONNECTED`），上报 `call_incoming`/`call_disconnected`（含 `reason`/`raw_reason`）；处理 `dial`/`hangup` 下行；来电时触发 `util_netled.active()` 活动快闪。
- `util_netled.lua`：网络状态指示灯模块。状态机：上电呼吸灯 → 注册网络后 `init()`(在线慢闪)/`waiting()`(等待连接快闪) → 收发短信/来电 `active()` 临时快闪；引脚/节奏可通过 `PWM_ID`/`LED_GPIO` 与各项 `*_duration`/`*_interval` 配置。

---

## 四、通信协议（JSON 行协议）

- **物理层**：串口波特率 `115200`，8N1；每条消息为一行 JSON，以 `\n` 结尾。
- **上行（LuatOS → Python）**：使用 `event` 字段标识事件类型。

| event | 方向 | 关键字段 | 说明 |
|-------|------|----------|------|
| `boot` | 设备→主机 | `imei`, `iccid`, `imsi`, `number`, `sim_ready`, `net_status`, `rssi`, `rsrp`, `rsrq`, `snr`, `model` | 设备启动/重启，用于注册（`number` 为本机号码，缺失时后端以 `imsi`(卡的标识) 兜底作为 `device_id`，极端再回退 `imei`）；`model` 为设备型号，用于系列标注 |
| `keepalive` | 设备→主机 | `timestamp`, `net_status`, `rssi`, `rsrp`, `rsrq`, `snr`, `imei`, `iccid`, `imsi`, `number`, `model` | 心跳保活（30s） |
| `netinfo` | 设备→主机 | `imei`, `imsi`, `iccid`, `number`, `csq`, `rssi`, `rsrp`, `rsrq`, `snr`, `net_status`, `simid`, `bands`, `band_count`, `model` | 网络信息周期上报（60s）及响应 `get_netinfo` 即时上报 |
| `log` | 设备→主机 | `level`, `tag`, `msg` | 设备运行日志转发 |
| `sms_received` | 设备→主机 | `phone`, `text`, `time`, `metas` | 收到新短信（`metas` 仅长短信含 `refNum`/`maxNum`/`seqNum` 分片信息） |
| `sms_sent_result` | 设备→主机 | `id`, `status`(`accepted`/`fail`), `error_code`, `reason`, `api_ok`, `api_return`, `long_sms`, `net_status`, `rssi`, `iccid` | 短信发送结果（`accepted` 表示已提交网络，`fail` 表示失败；`error_code`/`reason` 标识失败原因） |
| `call_incoming` | 设备→主机 | `phone` | 来电 |
| `call_disconnected` | 设备→主机 | `phone`, `reason`(`hangup`/`busy`/`no_answer`/`dial_failed`), `raw_reason` | 通话结束（`raw_reason` 为模组原始原因码）；后端补充 `direction`(`in`/`out`) 与 `duration`(秒) |

- **下行（Python → LuatOS）**：使用 `action` 字段标识命令类型。

| action | 方向 | 关键字段 | 说明 |
|--------|------|----------|------|
| `identify` | 主机→设备 | — | 握手，触发设备回复 `boot`（仅发现阶段，已启动模组收到后重发 `boot` 并点亮在线灯） |
| `send_sms` | 主机→设备 | `id`, `phone`, `text` | 发送短信 |
| `dial` | 主机→设备 | `id`, `phone` | 拨号 |
| `hangup` | 主机→设备 | — | 挂断 |
| `get_netinfo` | 主机→设备 | — | 请求设备立即上报一次 `netinfo` |

> `omnisms.py` 在发现新端口时会先发 `{"action":"identify"}` 触发握手；真实模组按自身节奏上报 `boot`，忽略未知命令。

---

## 五、数据库表结构

> 设备业务主键为 `device_id`：**本机号码(MSISDN) 优先**，SIM 卡未向模组暴露号码时**回退 IMSI（卡的标识）**，极端情况下再回退 IMEI（设备标识）。前端/API 统一以 `device_id` 寻址；`imei`、`imsi`、`phone` 作为冗余字段保留。

| 表 | 字段 | 说明 |
|----|------|------|
| `devices` | `device_id`(PK), `phone`, `imei`, `iccid`, `at_port`, `log_port`, `status`(`online`/`offline`/`error`), `remark`, `last_seen`, `rssi`, `rsrp`, `rsrq`, `snr`, `net_status`, `imsi`, `csq`, `bands`, `created_at` | 设备注册信息（`device_id` = 本机号码或 IMEI 兜底） |
| `sms_messages` | `id`(PK), `device_id`, `peer_phone`, `text`, `direction`(`in`/`out`), `status`(`pending`/`sent`/`failed`/`received`), `task_id`, `timestamp`, `created_at` | 短信记录（`peer_phone` 为对方号码） |
| `call_records` | `id`(PK), `device_id`, `peer_phone`, `direction`(`in`/`out`), `status`(`ringing`/`dialing`/`connected`/`disconnected`/`missed`), `start_time`, `end_time`, `duration`, `created_at` | 通话记录（`peer_phone` 为对方号码） |

> **迁移**：`database.py` 在初始化时若检测到旧版以 `imei` 为主键的 schema，会自动将旧表重命名为 `_legacy_*` 并重建为 `device_id` 主键，旧数据以原 `imei` 作为 `device_id` 保留（历史短信/通话仍可关联）。

---

## 六、启动方式

### 安装依赖
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 正常启动（连接真实 Air780E）
```bash
.venv/bin/python web.py --port 8000
```
- 引擎自动扫描 `/dev/ttyACM*`（VID/PID `19d1:0001`，覆盖 Air780E/EG/EP/EH）并注册设备。
- 默认监听 `127.0.0.1`，浏览器访问 `http://localhost:8000`。
- 需要局域网内其他机器访问时，加 `--host 0.0.0.0` 并**务必同时设置 `OMNISMS_PASSWORD`**。

### 命令行参数
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `127.0.0.1` | Web 监听地址（默认仅本机；需局域网访问时设 `OMNISMS_HOST=0.0.0.0` 或显式传参） |
| `--port` | `8000` | Web 监听端口 |
| `--ssl-cert` | 无 | SSL 证书文件路径（启用 HTTPS） |
| `--ssl-key` | 无 | SSL 私钥文件路径（启用 HTTPS） |

### 环境变量
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OMNISMS_HOST` | `127.0.0.1` | Web 监听地址。容器内必须绑定 `0.0.0.0`（Dockerfile CMD 已指定），是否暴露到局域网由 `docker run -p` 的宿主侧绑定决定 |
| `OMNISMS_PASSWORD` | 空 | 访问口令。**为空（默认）时服务免鉴权**，适用于仅本机访问；设置后启用共享口令会话，用于保护暴露到局域网的端口 |
| `OMNISMS_DISABLE_PYSERIAL` | 未设置 | 设为 `1` 强制禁用 pySerial 直连 |
| `OMNISMS_FORCE_DOCKER` | 未设置 | 设为 `1` 强制按 Docker 环境处理（禁用 pySerial） |
| `SSL_CERT_FILE` / `SSL_KEY_FILE` | 无 | HTTPS 证书与私钥路径 |

### 访问控制

本工具定位为**内网/本机单人使用**，**不提供多用户体系与角色权限**。是否启用鉴权取决于服务暴露范围：

| 部署形态 | 建议 |
|------|------|
| 原生运行（默认 `127.0.0.1`） | 免鉴权即可。注意：WebSocket 不受浏览器同源策略约束，任意网页都能直连本机端口，因此服务端**始终**校验 `Origin` 同源；`/api/env` 也始终免鉴权 |
| Docker 且仅本机访问（`-p 127.0.0.1:8000:8000`） | 免鉴权即可，端口不对外暴露 |
| Docker 暴露到局域网（`-p 8000:8000`，`build.sh run` 默认） | **必须设置 `OMNISMS_PASSWORD`**。短信/通话是会产生资费的外发能力，被滥用（轰炸/诈骗）可能导致 SIM 卡被运营商关停 |

启用口令后的行为：
- 未登录访问任意 REST 接口返回 `401`，前端自动弹出登录框。
- 登录后下发 `HttpOnly; SameSite=Strict` 会话 Cookie（HTTPS 下额外带 `Secure`）；同源 WebSocket 握手会自动携带该 Cookie，因此 REST 与 WebSocket 共用一套鉴权，令牌不出现在 URL 中。
- WebSocket 未通过校验时以 `1008` 拒绝握手；`/api/env` 与 `/static` 保持免鉴权。
- 会话仅存于内存，进程重启后需重新登录。

### 发送速率限制

独立于鉴权之外的兜底措施，即使环境完全可信也会生效（可挡住前端轮询缺陷或脚本失控导致的批量外发）：

| 接口 | 限速 |
|------|------|
| `POST /api/sms/send` | 每设备 20 条/分钟（突发 10 条） |
| `POST /api/call/dial` | 每设备 3 次/分钟 |

超限返回 `429`。

### HTTPS 访问

系统统一使用 HTTPS 访问（通过 `--ssl-cert` / `--ssl-key` 或环境变量 `SSL_CERT_FILE` / `SSL_KEY_FILE` 指定证书）。

```bash
# HTTPS 部署示例
.venv/bin/python web.py --host 0.0.0.0 --port 8000 \
    --ssl-cert /path/to/cert.pem --ssl-key /path/to/key.pem
```

### 设备接入方式（pySerial / WebSerial 二选一）

系统不再区分本地/远程，**pySerial 直连与 WebSerial 桥接自由切换、二选一**：

- **pySerial 直连**：引擎自动扫描 `/dev/ttyACM*` 注册设备（默认启用）。
- **WebSerial 桥接**：浏览器通过 WebSerial API 直连 USB 模组，经 `/ws/webserial` 将数据转发到后端。

**Docker 环境例外**：检测到运行在 Docker 容器内时，自动禁用 pySerial，仅支持 WebSerial 桥接（前端会提示「仅支持 WebSerial」）。也可显式设置 `OMNISMS_DISABLE_PYSERIAL=1` 强制禁用 pySerial。

### 使用管理脚本（`omnisms.sh`）

项目根目录提供 `omnisms.sh`，封装虚拟环境创建、依赖安装、守护进程启停与源码更新，无需手动操作 `.venv`。

```bash
chmod +x omnisms.sh

./omnisms.sh start             # 启动 (自动创建 .venv 并安装依赖, 守护进程方式)
./omnisms.sh stop              # 停止
./omnisms.sh restart           # 重启
./omnisms.sh status            # 查看运行状态
./omnisms.sh logs              # 实时查看日志 (tail -f omnisms.log)
./omnisms.sh update            # 从 GitHub 拉取最新源码覆盖本地并更新依赖, 然后停止 (需手动 start)
```

> 首次 `start` 若检测到 `.venv` 不存在，会自动执行 `python3 -m venv .venv` 并 `pip install -r requirements.txt`；若系统缺少 `python3-venv`，脚本会提示安装方式。

### systemd 开机自启

`omnisms.service` 为 systemd 服务模板（含 `__PROJECT_DIR__` / `__USER__` 占位符）。使用以下命令生成并安装（需 root）：

```bash
sudo ./omnisms.sh install-service   # 替换占位符写入 /etc/systemd/system/omnisms.service 并设为开机自启
sudo systemctl start omnisms        # 启动服务
sudo systemctl status omnisms       # 查看状态
sudo ./omnisms.sh uninstall-service # 卸载服务 (需 root)
```

服务以 `Type=simple` 运行，失败自动重启（`Restart=on-failure`）；日志写入 `omnisms.log`。

### Docker 部署

项目提供 `build.sh` 脚本与 `Dockerfile`，可一键构建并运行容器。Docker 环境下自动生成自签名证书并启用 HTTPS，且**自动禁用 pySerial，仅支持 WebSerial 桥接**（浏览器需与 USB 模组在同一台机器上）。

```bash
chmod +x build.sh

./build.sh              # 构建当前平台镜像 (自动检测 x86/ARM)
./build.sh run          # 构建并运行容器 (自动 HTTPS)
./build.sh logs         # 查看容器日志
./build.sh status       # 查看运行状态
./build.sh shell        # 进入容器终端
./build.sh cleanup      # 清理容器和镜像
```

`build.sh run` 等价于以下 `docker run` 命令（端口默认 `8000`，若被占用会提示输入新端口）：

```bash
docker run -d \
    --name omnisms \
    --privileged \
    --restart unless-stopped \
    -p 8000:8000 \
    -v omnisms-logs:/app/logs \
    -v omnisms-db:/app/data \
    -e TZ=Asia/Shanghai \
    omnisms:latest
```

> 说明：
> - 容器内服务监听 `8000` 端口，映射到宿主机 `8000` 端口，访问地址为 `https://localhost:8000`。
> - 首次访问会因自签名证书触发浏览器安全提示，点击「高级」→「继续前往」即可。
> - 数据持久化在 `omnisms-logs`（日志）与 `omnisms-db`（SQLite 数据库）两个 Docker 卷中。
> - 设备接入依赖浏览器 WebSerial API（需 Chromium 内核浏览器），无需在容器内挂载 USB 设备（因此容器不再使用 `--privileged`）。
> - **安全**：`-p 8000:8000` 会把端口发布到宿主机的 `0.0.0.0`，即**整个局域网可达**。`build.sh run` 会交互式询问访问口令，也可提前设置 `OMNISMS_PASSWORD=xxx ./build.sh run`。若只需本机访问，改用 `-p 127.0.0.1:8000:8000` 即可完全不暴露。

---

## 七、REST API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 主页面 |
| GET | `/api/env` | 运行环境信息 `{is_docker, pyserial_disabled, auth_required}`（前端据此决定连接模式与是否弹登录框；始终免鉴权） |
| POST | `/api/login` | 共享口令登录 `{password}`，成功后下发 HttpOnly 会话 Cookie；未启用口令时返回 `required=false` |
| POST | `/api/logout` | 注销当前会话 |
| GET | `/api/devices` | 设备列表（在线 + 数据库离线/备注合并） |
| GET | `/api/devices/{device_id}` | 单设备详情（`device_id` = 本机号码或 IMEI 兜底） |
| POST | `/api/disconnect` | 删除设备：从引擎内存移除并断开连接，同时从数据库彻底删除 `{device_id}` |
| POST | `/api/devices/remark` | 保存设备备注 `{device_id, remark}` |
| POST | `/api/scan` | 手动扫描：对每个端口独立探测，每个端口最多等待 `duration` 秒（默认 15） |
| POST | `/api/scan/auto/start` | 启动后台自动扫描（引擎启动时已默认开启） |
| POST | `/api/scan/auto/stop` | 停止后台自动扫描（不影响已注册设备） |
| POST | `/api/scan/stop` | 提前停止正在进行的手动扫描 |
| GET | `/api/scan/status` | 查询扫描状态（`scanning` / `auto_scanning`） |
| POST | `/api/sms/send` | 发送短信 `{device_id, phone, text}`（下发失败返回 `502`；超出限速返回 `429`） |
| GET | `/api/sms/conversations?device_id=` | 短信记录（扁平，peer_phone 原样返回；聚合与展示由前端完成） |
| GET | `/api/sms/messages?device_id=&peer_phone=` | 某原始号码的全部消息（精确匹配） |
| POST | `/api/sms/purge` | 清空指定号码短信记录 `{device_id?, phone, confirm, dry_run}`（需 `confirm=true`，含事务回滚） |
| GET | `/api/calls?device_id=` | 通话记录（扁平，peer_phone 原样返回；聚合与展示由前端完成） |
| GET | `/api/calls/conversations?device_id=` | 通话记录（扁平，同 `/api/calls`） |
| POST | `/api/call/dial`（`/api/call/make`） | 拨号 `{device_id, phone}`，返回 `{success, task_id, message}`；超出限速返回 `429` |
| POST | `/api/call/hangup` | 挂断 `{device_id}` |
| GET | `/api/logs` | 历史日志（过滤/分页）。单次扫描同时返回 `total` 与 `total_exact`；`total_exact=false` 时 `total` 为下界，应显示为「≥ N」 |
| GET | `/api/logs/cache` | 最近日志缓存 |
| GET | `/api/logs/files` | 日志文件列表 |
| POST | `/api/logs/clear-cache` | 清空内存日志缓存 |

### WebSocket 事件（`/ws/log`）
前端实时接收以下 `type`：
- `log`：`{timestamp, level, logger, message, module}`
- `device_event`：`boot` / `keepalive` / `disconnect`
- `sms_event`：`sms_received` / `sms_sent_result`
- `call_event`：`call_incoming` / `call_disconnected`（`call_disconnected` 含 `direction` 与真实 `duration` 秒数）

### WebSocket 桥接（`/ws/webserial`）
WebSerial 桥接模式下浏览器经此端点桥接 USB 数据，协议：
- 浏览器 → 后端：`{"type":"raw_line","data":"<原始JSON行>"}`、`{"type":"register",...}`、`{"type":"ping"}`
- 后端 → 浏览器：`{"type":"registered","bridge_id":...}`、`{"type":"command","action":...}`、`{"type":"pong"}`、`{"type":"device_registered","device_id":...}`

---

## 八、配置项（`omnisms.py` 的 `Config`）

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `BAUD_RATE` | `115200` | 串口波特率 |
| `SCAN_INTERVAL_SEC` | `3.0` | 端口扫描间隔 |
| `SERIAL_TIMEOUT` | `1.0` | 串口读取超时 |
| `BOOT_TIMEOUT` | `5.0` | 等待首个固件事件超时（不限于 boot，也接受 keepalive/log 等存活事件） |
| `BOOT_RETRY_TIMEOUT` | `10.0` | 识别到存活事件后补发 identify、延长等待 boot 的超时 |
| `RESCAN_KNOWN_GROUP_SEC` | `60.0` | 已确定但注册失败的设备组，降低频率重新探测的间隔 |
| `LUAT_VID` / `LUAT_PID` | `0x19D1` / `0x0001` | Air780 家族 USB 过滤（历史字段，始终纳入匹配） |
| `LUAT_VID_PID_LIST` | `[(0x19D1, 0x0001)]` | 兼容的 USB VID/PID 列表，覆盖 Air780E/EG/EP/EH；如需支持更多变体在此追加 `(vid, pid)` 元组 |
| `PORT_PATTERN` | `/dev/ttyACM\d+` | 真实串口匹配 |
| `DISABLE_PYSERIAL` | `False` | 禁用 pySerial 直连（Docker 环境由 `web.py` 自动置为 `True`） |
| `DB_PATH` | `omnisms.db` | 数据库路径 |
| `LOG_DIR` / `LOG_FILE` | `logs` / `logs/omnisms.log` | 日志目录/文件 |

---

## 九、已知限制 / 备注

- 设备仅由引擎自动发现注册（真实 USB 串口），不支持手动指定串口路径。
- Docker 环境下 pySerial 被禁用，设备接入依赖浏览器 WebSerial API（需 Chromium 内核浏览器，且浏览器与 USB 模组在同一台机器上）。
- 真实硬件部署时，需将 `Luatos/` 下四个 `.lua`（`main` / `sms_handler` / `call_handler` / `util_netled`）烧录到 Air780 家族模组，并按硬件设置 `main.lua` 中的 `DEVICE_MODEL`，随后运行 `sys.run()`。

---

## 十、设备系列支持（Air780E / Air780EG / Air780EP / Air780EH）

OmniSMS 在不重复开发的前提下，统一兼容 **Air780E、Air780EG、Air780EP、Air780EH** 四个系列。

> 核心结论：**这四个系列的底层逻辑与架构完全一致**，共用同一套 LuatOS API、相同的 USB 枚举方式与相同的 JSON 串口协议。因此 OmniSMS 采用「统一处理 + 系列标注」策略，业务代码零分支，仅在元数据层面区分系列，便于前端展示与后续按系列扩展（如特定射频参数）。

### 1. 各系列特性差异说明

差异仅存在于**芯片平台、外设资源与封装**层面，**不影响短信 / 通话 / 网络诊断业务**，因此无需在业务代码中区分。

| 系列 | 芯片平台 | 网络制式 | 封装 | 关键差异 | 业务影响 |
|------|----------|----------|------|----------|----------|
| **Air780E** | EC618 | LTE Cat.1 bis | LCC + 32pin LGA | 基础版，单卡单待 | 无（基准） |
| **Air780EG** | EC618 | LTE Cat.1 bis | LCC + 32pin LGA | 集成 **GNSS** 定位 | 无（定位为独立功能，不干扰短信/通话） |
| **Air780EP** | EC718 | LTE Cat.1 bis | LCC + 56pin LGA | 更多 GPIO / 外设资源 | 无（资源差异不影响本系统使用的接口） |
| **Air780EH** | EC718 | LTE Cat.1 bis | LCC + 56pin LGA | EC718 + **GNSS** | 无 |

> 若未来某系列需要差异化参数（例如特定频段优选、功耗策略），可在 `AIR780_SERIES` 元数据表中按系列追加字段，并在对应逻辑处读取——现有「零分支」结构可平滑演进为「按系列读取配置」。

### 2. 配置与接入指引

#### 2.1 主机侧（无需改动即可兼容）

- 默认配置已覆盖四系列：`Config.LUAT_VID_PID_LIST` 默认含 `19d1:0001`，端口发现自动识别全部系列。
- 若某变体使用不同的 VID/PID，在 `Config` 中追加元组即可，例如：
  ```python
  LUAT_VID_PID_LIST: list = field(default_factory=lambda: [(0x19D1, 0x0001), (0x19D1, 0x0002)])
  ```
- 若需基于 IMEI TAC 精确标注系列，在 `omnisms.py` 的 `IMEI_TAC_TO_SERIES` 中补充：
  ```python
  IMEI_TAC_TO_SERIES = {
      "12345678": "Air780EG",
      "87654321": "Air780EP",
  }
  ```

#### 2.2 固件侧（按硬件设置型号）

四个系列烧录**同一份** `LuatOS/` 固件。唯一需要按硬件调整的是 `main.lua` 顶部的常量：

```lua
-- 设备型号: 烧录到不同 Air780 系列时修改此处即可
local DEVICE_MODEL = "Air780E"   -- 改为 "Air780EG" / "Air780EP" / "Air780EH"
```

- 该值会通过 `boot` / `keepalive` / `netinfo` 事件上报给主机，用于系列标注。
- 若保持 `"Air780E"` 或留空 `""`，主机将退而使用 IMEI TAC 推断；推断失败则标注为通用 `"Air780"`，**业务功能不受影响**。

#### 2.3 部署步骤

1. 将 `LuatOS/` 下四个 `.lua`（`main.lua` / `sms_handler.lua` / `call_handler.lua` / `util_netled.lua`）烧录到目标模组，并按硬件设置 `DEVICE_MODEL`。
2. 模组通过 USB 接入 Linux 主机，确认枚举出 `/dev/ttyACM*`（权限通常需 `dialout` 组或 `sudo`）。
3. 启动 OmniSMS：
   ```bash
   .venv/bin/python web.py --host 0.0.0.0 --port 8000
   ```
4. 引擎自动扫描并注册设备；在 Web 界面「设备列表」中可看到 `series` / `model` 字段正确标注为对应系列。
5. 如未自动发现，可点击「手动扫描」；如系列标注为通用 `"Air780"`，按上文补充 `IMEI_TAC_TO_SERIES` 或确认固件 `DEVICE_MODEL` 已正确设置。

#### 2.4 验证要点

- 设备列表 API `/api/devices` 返回的每个设备含 `series` 与 `model` 字段。
- 日志中出现 `Air780 family port groups` / `Air780 family port topology changed` 等家族化提示。
- 短信收发、通话控制、网络诊断在四系列上行为一致。
