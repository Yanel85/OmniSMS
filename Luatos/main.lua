--[[
    main.lua - OmniSMS Air780 家族(Air780E / Air780EG / Air780EP / Air780EH)固件主入口
    功能: 系统初始化、串口通信、模块加载、心跳保活
    平台: LuatOS v1.0.x / v2.0.x (Air780E/EG: EC618, Air780EP/EH: EC718)
    说明: 各系列底层逻辑与架构完全一致, 同一份固件即可运行; 仅需按硬件设置 DEVICE_MODEL
]]

-- ==================== 项目元信息 ====================
PROJECT = "OmniSMS"
VERSION = "2.0.0"

-- 设备型号: 烧录到不同 Air780 系列时修改此处即可(如 "Air780EG" / "Air780EP" / "Air780EH")
-- 主机据此识别设备系列; 留空("")则主机按 IMEI TAC 推断(业务仍完全可用)。
local DEVICE_MODEL = "Air780E"

-- 加载系统库
sys = require("sys")

-- 加载网络状态指示灯控制模块(上电即开始呼吸灯)
local netled = require("util_netled")

log.info("main", PROJECT, VERSION)

-- ==================== 串口通信层 ====================
-- 设计原则: 直接收发, 不使用自定义接收缓冲区 / 发送队列 / 缓冲机制
--   发送: 编码后在 sys.taskInit 中直接 uart.write, 重试等待也在 task 上下文中安全执行
--   接收: uart 回调读取后按 \n 直接切分, 逐行用 sys.taskInit 异步分发
-- 仅保留 rx_pending 用于跨次读取的不完整行重组(非通用缓冲区)
local UART_ID = uart.VUART_0
local BAUD_RATE = 115200
local TX_RETRY_COUNT = 3
local TX_RETRY_DELAY_MS = 50

-- 跨次读取的不完整行残片(仅用于行协议重组, 不是通用接收缓冲区)
local rx_pending = ""

-- 动作回调(由 main 注册)
local on_action_callback = nil

-- 直接写入一帧(带重试), 无发送队列
local function write_frame(full_msg)
    local offset = 1
    local retries = 0
    while offset <= #full_msg do
        local chunk = full_msg:sub(offset)
        local ok, written = pcall(uart.write, UART_ID, chunk)
        local progress = 0
        if ok and written == true then
            -- 部分固件只返回 boolean；true 表示整段已交给 UART。
            progress = #chunk
        elseif ok and type(written) == "number" and written > 0 and written <= #chunk then
            progress = written
        end

        if progress > 0 then
            offset = offset + progress
            retries = 0
        else
            retries = retries + 1
            log.warn("comm.tx", "uart.write failed, retry:", retries,
                "written:", tostring(written), "remaining:", #chunk)
            if retries >= TX_RETRY_COUNT then
                return false
            end
            sys.wait(TX_RETRY_DELAY_MS)
        end
    end
    return true
end

-- 直接发送 table 到主机(编码后在 task 中写入, 不进队列)
local function send_to_host(data)
    if not data then
        log.warn("comm", "send_to_host: nil data")
        return false
    end

    local ok, json_str = pcall(json.encode, data)
    if not ok or not json_str then
        log.error("comm", "json encode failed")
        return false
    end

    -- 异步发送: 在 task 中直接写入 UART, 不使用发送队列/缓冲区
    -- (task 上下文使重试时的 sys.wait 可安全执行)
    sys.taskInit(function()
        local wok = write_frame(json_str .. "\n")
        if not wok then
            log.error("comm.tx", "frame dropped after retries")
        end
    end)
    return true
end

-- 将运行日志转发到主机(Python Daemon)
local function log_to_host(level, tag, msg)
    return send_to_host({
        event = "log",
        level = level or "info",
        tag = tostring(tag or ""),
        msg = tostring(msg or "")
    })
end

-- 处理接收到的完整行(JSON 字符串)
local function handle_received_line(line)
    if not line or line == "" then return end

    log.debug("comm.rx_raw", line)

    local ok, msg = pcall(json.decode, line)

    if not ok or not msg then
        log.warn("comm.rx", "invalid json:", line:sub(1, 50))
        return
    end

    if type(msg) ~= "table" then
        log.warn("comm.rx", "message is not a JSON object:", line:sub(1, 50))
        return
    end

    if msg.action then
        -- 下行动作命令 (Python -> LuatOS)
        if on_action_callback then
            on_action_callback(msg)
        else
            log.warn("comm", "no action handler registered")
        end
    else
        log.warn("comm", "unknown message type")
    end
end

-- UART 接收回调: 读取后直接切分并处理, 不累积到自定义缓冲区
local function uart_receive_callback(id, len)
    local data = uart.read(id, len)

    if not data or #data == 0 then
        return
    end

    -- 拼接上次残留的不完整行, 得到本批可处理文本
    local batch = rx_pending .. data
    rx_pending = ""

    -- 按 \n 提取完整行, 逐行异步分发(用 task 机制避免阻塞 UART 中断)
    local start = 1
    while true do
        local idx = batch:find("\n", start, true)
        if not idx then
            -- 剩余部分(可能不完整)留作下次残片, 不做缓冲堆积
            rx_pending = batch:sub(start)
            break
        end

        local line = batch:sub(start, idx - 1)
        start = idx + 1

        if line ~= "" then
            sys.taskInit(function()
                local ok, err = pcall(handle_received_line, line)
                if not ok then
                    log.error("comm.dispatch", tostring(err))
                end
            end)
        end
    end
end

-- 注册动作命令回调
local function set_action_handler(callback)
    on_action_callback = callback
end

-- 初始化 UART 通信层
local function init_comm()
    log.info("comm", "init vUART_0 at", BAUD_RATE)
    uart.setup(UART_ID, BAUD_RATE)

    -- 清空残留行残片
    rx_pending = ""

    -- 注册 UART 接收回调 (先 setup 再 on)
    uart.on(UART_ID, "receive", uart_receive_callback)

    log.info("comm", "ready")
end

-- 暴露给其他 handler 模块使用
_G.comm = {
    send_to_host = send_to_host,
    log_to_host = log_to_host,
    set_action_handler = set_action_handler,
    init = init_comm,
    netled = netled,   -- 供 sms_handler / call_handler 触发快闪
}

-- ==================== 公共辅助函数 ====================
-- 判断网络是否已注册(1/5=正常注册, 6/7=仅短信注册, 短信业务同样可用)
local function is_registered(status)
    return status == 1 or status == 5 or status == 6 or status == 7
end

-- 读取当前信号快照(网络状态 + 各信号量, 缺失以 -999 兜底)
-- 供 netinfo / boot / keepalive 监控任务复用, 避免重复调用 mobile.*
local function read_signal_snapshot()
    return {
        net_status = mobile.status() or 0,
        rssi = mobile.rssi() or -999,
        rsrp = mobile.rsrp() or -999,
        rsrq = mobile.rsrq() or -999,
        snr = mobile.snr() or -999,
    }
end

-- 暴露 is_registered 供其他 handler 模块复用(避免各模块重复魔法数字判断)
_G.comm.is_registered = is_registered

-- ==================== 指示灯状态联动 ====================
-- 主机是否已握手(identify): 握手后才视为"在线", 否则仅"已注册网络, 等待连接"
local host_handshaked = false

-- 网络已注册时的指示灯状态:
--   主机已握手 -> 在线慢闪; 主机未握手 -> 等待连接快闪
local function netled_set_online_state()
    if host_handshaked then
        netled.init()
    else
        netled.waiting()
    end
end

-- ==================== 网络信息模块 ====================
-- 参考官方文档: https://docs.openluat.com/air780e/luatos/app/common/mobile/
-- 功能:
--   1. 采集 IMEI / IMSI / ICCID / 本机号码(number) / CSQ / RSSI / RSRQ / RSRP / SNR / 频段 等网络参数
--   2. 通过串口 JSON 以 event=netinfo 上报到后端
--   3. 响应主机下发的 action=get_netinfo, 立即上报一次
-- 说明:
--   - 4G 模块 CSQ 并不能完全代表信号强度, 需综合 rssi/rsrq/rsrp/snr 判断
--   - 频段通过 mobile.getBand(zbuff) 读取, 返回当前工作频段编号数组
--   - 本机号码(MSISDN) 通过 mobile.number(0) 读取, 依赖 SIM 卡是否向模组暴露,
--     未暴露时返回空字符串; 此时后端以 IMSI(卡的标识) 兜底作为设备标识

-- mobile.getBand 频段缓冲容量(个), 官方示例用 40
local BAND_BUFF_SIZE = 40

-- 获取本机号码(MSISDN), 依赖 SIM 卡是否向模组暴露, 失败/未暴露返回空字符串
local function netinfo_get_number()
    local ok, num = pcall(mobile.number, 0)
    if ok and num and num ~= "" then
        return tostring(num)
    end
    return ""
end

-- 读取当前工作频段列表(数组), 失败返回空表
local function netinfo_read_bands()
    local list = {}
    local ok, buff = pcall(zbuff.create, BAND_BUFF_SIZE)
    if not ok or not buff then
        return list
    end
    ok = pcall(mobile.getBand, buff)
    if not ok then
        return list
    end
    local used = 0
    pcall(function() used = buff:used() or 0 end)
    for i = 0, used - 1 do
        local b = buff[i]
        if b then
            list[#list + 1] = b
        end
    end
    return list
end

-- 采集所有网络参数, 返回 table
local function netinfo_collect()
    local sig = read_signal_snapshot()
    local info = {}
    info.imei       = mobile.imei() or ""
    info.imsi       = mobile.imsi() or ""
    info.iccid      = mobile.iccid() or ""
    info.number     = netinfo_get_number()   -- 本机号码(MSISDN), 作为设备业务标识, 缺失时后端回退 IMSI(卡的标识)
    info.csq        = mobile.csq() or -999       -- 4G 模块 CSQ 仅供参考
    info.rssi       = sig.rssi
    info.rsrq       = sig.rsrq
    info.rsrp       = sig.rsrp
    info.snr        = sig.snr
    info.net_status = sig.net_status
    -- SIM 卡槽(可能返回 nil)
    local simid_ok, simid = pcall(mobile.simid)
    info.simid = simid_ok and simid or -1
    -- 当前工作频段列表
    info.bands = netinfo_read_bands()
    info.band_count = #info.bands
    -- 设备型号(供主机识别系列, 如 Air780EG / Air780EP / Air780EH)
    info.model = DEVICE_MODEL
    return info
end

-- 采集并发送 netinfo 事件到主机
local function netinfo_report()
    local info = netinfo_collect()
    info.event = "netinfo"
    info.timestamp = os.time()
    local ok = send_to_host(info)
    if ok then
        log.info("netinfo", "reported imsi:", info.imsi, "csq:", info.csq,
            "bands:", info.band_count, "net_status:", info.net_status)
    else
        log.error("netinfo", "report failed")
    end
end

-- 响应主机下发的 get_netinfo 动作, 立即上报一次
local function netinfo_handle_get_netinfo(msg)
    netinfo_report()
end

-- 初始化网络信息模块: 启动后稍等网络就绪上报一次, 并周期(60s)上报
local function netinfo_init()
    -- 启动后稍等网络就绪, 上报一次(周期上报由下方统一监控任务负责)
    sys.taskInit(function()
        sys.wait(3000)
        netinfo_report()
    end)
end

-- ==================== 看门狗配置 ====================
if wdt then
    pcall(wdt.init, 9000)   -- 9秒超时 (pcall 防异常)
    sys.timerLoopStart(function()
        if wdt then pcall(wdt.feed) end
    end, 3000)  -- 3秒喂狗
end

-- ==================== 网络与DNS配置 ====================
pcall(function()
    socket.setDNS(0, 1, "119.29.29.29")   -- DNS1: 腾讯 (adapter=0 移动网络)
    socket.setDNS(0, 2, "223.5.5.5")       -- DNS2: 阿里
end)

-- 网络自动恢复：SIM 检查10秒、小区信息关闭、搜网最多5秒、无网60秒恢复
local auto_recover_ok, auto_recover_err = pcall(mobile.setAuto, 10000, 0, 5, nil, 60000)
if not auto_recover_ok then
    log.error("main.mobile", "setAuto failed:", tostring(auto_recover_err))
else
    log.info("main.mobile", "setAuto configured: sim=10000ms, cell=off, search=5s, network=60000ms")
end

-- ==================== 模块加载 ====================
local sms_handler = require("sms_handler")
local call_handler = require("call_handler")

-- ==================== 初始化各业务模块 ====================

-- 1. 初始化串口通信层
-- 用 pcall 包裹初始化, 避免 uart.setup 抛错导致 main.lua 在 sys.run() 前崩溃(否则看门狗反复重启, VUART 全程静默)
local ok_init, err_init = pcall(init_comm)
if not ok_init then
    log.error("main", "comm init failed:", tostring(err_init))
end

-- 发送 Boot 事件函数由后面的实现赋值；先声明以便动作回调安全引用
local send_boot_event

-- 2. 注册 UART 动作分发器
-- 当从主机收到 action 命令时，根据类型路由到对应处理器
set_action_handler(function(msg)
    local action = msg.action

    log.info("main.dispatcher", "action:", action)

    if action == "send_sms" then
        -- 转发到 SMS 处理器
        sms_handler.handle_send_sms(msg)

    elseif action == "dial" then
        -- 转发到电话处理器
        call_handler.handle_dial(msg)

    elseif action == "hangup" then
        -- 转发到电话处理器
        call_handler.handle_hangup(msg)

    elseif action == "get_netinfo" then
        -- 主机请求网络信息, 立即上报一次
        netinfo_handle_get_netinfo(msg)

    elseif action == "identify" then
        -- 主机握手: 重新上报 boot 事件, 便于已启动的模组被重新发现
        log.info("main.dispatcher", "identify -> resend boot")
        host_handshaked = true
        netled.init()   -- 主机已连接, 指示灯转为在线慢闪
        send_boot_event()

    else
        log.warn("main.dispatcher", "unknown action:", action or "nil")
    end
end)

-- 3. 初始化 SMS 处理器(注册短信接收回调)
sms_handler.init()

-- 4. 初始化电话处理器(注册来电/挂断事件监听)
call_handler.init()

-- 5. 初始化网络信息模块(采集 IMEI/IMSI/ICCID/CSQ/频段等并周期上报)
netinfo_init()

-- ==================== 发送 Boot 事件 ====================

-- 向主机上报 boot 事件 (启动时使用, 也用于响应主机下发的 identify 握手)
send_boot_event = function()
    -- 获取设备标识和一次性网络采样
    local imei = mobile.imei()
    local iccid = mobile.iccid()
    local imsi = mobile.imsi() or ""   -- 卡的标识(IMSI), 作为设备业务标识的兜底
    local number = netinfo_get_number()  -- 本机号码(MSISDN), 优先业务标识
    local sig = read_signal_snapshot()
    local net_status = sig.net_status
    -- 1/5 为正常注册，6/7 为仅短信注册，对短信业务同样可用
    local sim_ready = is_registered(net_status)

    if not imei or not iccid then
        log.warn("main.boot", "identity unavailable, imei:", tostring(imei), "iccid:", tostring(iccid))
    end
    if not sim_ready then
        log.warn("main.boot", "network not registered, status:", net_status)
    else
        log.info("main.boot", "network registered, status:", net_status)
    end

    log_to_host("info", "main.boot", "imei: " .. tostring(imei) ..
        " iccid: " .. tostring(iccid) .. " net_status: " .. tostring(net_status) ..
        " sim_ready: " .. tostring(sim_ready))

    -- 发送启动/保活事件给主机
    local boot_event = {
        event = "boot",
        imei = imei,
        iccid = iccid,
        imsi = imsi,
        number = number,
        sim_ready = sim_ready,
        net_status = net_status,
        rssi = sig.rssi,
        rsrp = sig.rsrp,
        rsrq = sig.rsrq,
        snr = sig.snr,
        model = DEVICE_MODEL,
    }

    local ok = send_to_host(boot_event)
    if ok then
        log.info("main.boot", "sent boot event successfully")
    else
        log.error("main.boot", "failed to send boot event")
    end
end

-- 启动时发送 Boot 事件：等待网络注册，超时也上报真实状态
sys.taskInit(function()
    local registered = false
    for _ = 1, 60 do  -- 最多等待 30 秒
        if is_registered(mobile.status() or 0) then
            registered = true
            netled_set_online_state()   -- 注册网络, 开始闪烁(等待连接/在线)
            break
        end
        sys.wait(500)
    end

    send_boot_event()
    if registered then
        log.info("main", "OmniSMS firmware ready, network registered")
    else
        log.warn("main", "OmniSMS firmware started, network registration timeout")
    end
end)

-- ==================== 统一系统监控任务(状态日志 + 心跳 + 网络信息) ====================
-- 每次循环只读取一次信号快照, 按时间阈值分别上报(注册时 30s 节奏, 未注册时 10s 节奏), 减少重复的 mobile.* 调用
sys.taskInit(function()
    local now0 = os.time()
    local last_keepalive = now0
    local last_netinfo = now0
    local prev_registered = false
    while true do
        local sig = read_signal_snapshot()
        local registered = is_registered(sig.net_status)

        -- 网络注册状态跳变(未注册 -> 已注册): 启动/恢复指示灯闪烁
        if registered and not prev_registered then
            netled_set_online_state()
        end
        prev_registered = registered

        -- 网络状态日志
        if registered then
            log.debug("main.net", "registered status:", sig.net_status, "rssi:", sig.rssi,
                "rsrp:", sig.rsrp, "rsrq:", sig.rsrq, "snr:", sig.snr)
        else
            log.warn("main.net", "not registered status:", sig.net_status, "rssi:", sig.rssi,
                "rsrp:", sig.rsrp, "rsrq:", sig.rsrq, "snr:", sig.snr)
        end

        -- 每 30s 上报 keepalive(携带 imei/iccid/imsi 便于主机识别设备)
        local now = os.time()
        if now - last_keepalive >= 30 then
            last_keepalive = now
            send_to_host({
                event = "keepalive",
                timestamp = now,
                net_status = sig.net_status,
                rssi = sig.rssi,
                rsrp = sig.rsrp,
                rsrq = sig.rsrq,
                snr = sig.snr,
                imei = mobile.imei(),
                iccid = mobile.iccid(),
                imsi = mobile.imsi() or "",
                number = netinfo_get_number(),
                model = DEVICE_MODEL,
            })
            log_to_host("debug", "main.heartbeat", "keepalive status=" .. tostring(sig.net_status) ..
                " rssi=" .. tostring(sig.rssi) .. " rsrp=" .. tostring(sig.rsrp) ..
                " rsrq=" .. tostring(sig.rsrq) .. " snr=" .. tostring(sig.snr))
        end

        -- 每 60s 上报一次完整网络信息(初始 3s 上报由 netinfo_init 负责)
        if now - last_netinfo >= 60 then
            last_netinfo = now
            netinfo_report()
        end

        -- 根据网络状态调整检查频率
        if registered then
            sys.wait(30000)  -- 正常状态30秒检查一次
        else
            sys.wait(10000)  -- 异常状态10秒检查一次
        end
    end
end)

-- ==================== 启动事件循环 ====================
log.info("main", "starting event loop...")

sys.run()  -- 进入 LuatOS 主循环（永不返回）
