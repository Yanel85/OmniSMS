--[[
    sms_handler.lua - SMS 消息处理模块
    负责接收短信(上报后端)、发送短信(响应主机下行)
    依赖: main.lua 暴露的 _G.comm (send_to_host)
    设计: 不使用队列, 发送直接丢到 task 里调用 sms.send / sms.sendLong
    长短信: 接收端由 sms.autoLong 自动合并为一条 txt; 发送端超单条限制走 sms.sendLong
]]

local M = {}
local sys = require("sys")
local comm = _G.comm  -- main.lua 暴露的串口通信层

-- 单条短信最大字节数(UTF-8): 超过则走长短信 sms.sendLong
local SMS_SEGMENT_BYTES = 140
-- 长短信上限(字节): sendLong 单分片≤134字节, 最多128片, 这里限制总字节数
local SMS_MAX_BYTES = 1000
-- 接收侧安全上限(字节): 单条短信内容长度保护(已无接收缓冲区上限限制)
local SMS_RECV_MAX_BYTES = 3000

-- ==================== 辅助函数 ====================

-- 生成 UUID (简化版, 主机未带 id 时使用)
local function generate_uuid()
    local template = "xxxxxxxx-xxxx-4xxx-yxx-xxxxxxxxxxxx"
    return template:gsub("[xy]", function(c)
        local v = (c == 'x') and math.random(0, 0xf) or math.random(0, 0xb)
        return string.format('%x', v)
    end)
end

-- 从短信元数据取时间(YYYY-MM-DD HH:MM:SS), 取不到则用当前时间
local function get_sms_time(metas)
    if type(metas) ~= "table" then return os.date("%Y-%m-%d %H:%M:%S") end
    local y, m, d, h, mi, s = tonumber(metas.year), tonumber(metas.mon),
        tonumber(metas.day), tonumber(metas.hour), tonumber(metas.min), tonumber(metas.sec)
    if y and m and d and h and mi and s then
        return string.format("%04d-%02d-%02d %02d:%02d:%02d", y + 2000, m, d, h, mi, s)
    end
    return os.date("%Y-%m-%d %H:%M:%S")
end

-- 长短信分片信息(仅长短信时 metas 含 refNum/maxNum/seqNum)
local function long_sms_metas(metas)
    if type(metas) ~= "table" then return nil end
    if metas.refNum or metas.maxNum or metas.seqNum then
        return { refNum = metas.refNum, maxNum = metas.maxNum, seqNum = metas.seqNum }
    end
    return nil
end

-- 上报发送结果到主机
local function send_sms_result(task_id, status, error_code, reason, extra)
    local result = {
        event = "sms_sent_result",
        id = task_id,
        status = status,
        error_code = error_code,
        reason = reason,
    }
    if extra then
        for key, value in pairs(extra) do
            result[key] = value
        end
    end
    comm.send_to_host(result)
end

-- ==================== 上行事件: 短信接收 ====================

--[[
    注册短信接收回调
    收到新短信 -> 安全检查 -> 通过串口 JSON 上报后端
    长短信已由 sms.autoLong 自动合并为一条 txt
]]
M.init_receiver = function()
    log.info("sms_handler", "registering SMS receiver callback")

    sms.setNewSmsCb(function(phone, data, metas)
        local num = tostring(phone or "")
        local txt = tostring(data or "")

        -- 1. 基本安全检查
        if num == "" or txt == "" then
            log.warn("sms_handler", "收到无效短信")
            return
        end

        -- 2. 短信长度限制(长短信合并后可能较长, 但需小于 UART 接收缓冲)
        if #txt > SMS_RECV_MAX_BYTES then
            log.warn("sms_handler", "短信内容过长, bytes:", #txt)
            return
        end

        log.info("sms_handler", num, txt, txt:toHex())

        -- 收到短信: 指示灯临时快闪
        if comm.netled then comm.netled.active(2000) end

        -- 通过串口 JSON 上报到后端
        comm.send_to_host({
            event = "sms_received",
            phone = num,
            text = txt,
            time = get_sms_time(metas),
            metas = long_sms_metas(metas),
        })
    end)

    log.info("sms_handler", "SMS receiver ready")
end

-- ==================== 下行动作: 短信发送 ====================

--[[
    处理来自主机的 send_sms 命令
    @param msg: JSON 消息 {action="send_sms", id=uuid, phone=..., text=...}
    超单条限制(140字节)自动走长短信 sms.sendLong, 否则走普通 sms.send
]]
M.handle_send_sms = function(msg)
    local target_phone = tostring(msg.phone or "")
    local content = tostring(msg.text or "")
    local task_id = msg.id or generate_uuid()

    if target_phone == "" or content == "" then
        log.error("sms_handler.tx", "missing phone or text")
        send_sms_result(task_id, "fail", -1, "missing_phone_or_text")
        return
    end

    if #content > SMS_MAX_BYTES then
        log.error("sms_handler.tx", "text too long, bytes:", #content)
        send_sms_result(task_id, "fail", -4, "text_over_limit", { text_bytes = #content })
        return
    end

    -- 直接丢到 task 里发送, 不需要队列
    -- sms.sendLong 必须在 task 中调用并 .wait() 等待结果
    sys.taskInit(function()
        local net_status = mobile.status() or 0
        if not comm.is_registered(net_status) then
            log.error("sms_handler.tx", "network not registered, status:", net_status)
            send_sms_result(task_id, "fail", -5, "network_not_registered", {
                net_status = net_status,
                rssi = mobile.rssi() or -999,
            })
            return
        end

        -- 发送短信: 指示灯临时快闪
        if comm.netled then comm.netled.active(2000) end

        local is_long = #content > SMS_SEGMENT_BYTES
        local ok, ret
        if is_long then
            -- 长短信: 同步发送, 等待最终结果
            ok, ret = pcall(function()
                return sms.sendLong(target_phone, content).wait()
            end)
        else
            -- 普通短信: 异步提交
            ok, ret = pcall(sms.send, target_phone, content)
        end
        local accepted = ok and ret == true

        send_sms_result(task_id, accepted and "accepted" or "fail",
            accepted and 0 or (ok and -3 or -2), nil, {
                api_ok = ok,
                api_return = tostring(ret),
                long_sms = is_long,
                net_status = net_status,
                rssi = mobile.rssi() or -999,
                iccid = tostring(mobile.iccid() or "unknown"),
            })

        if accepted then
            log.info("sms_handler.tx", "send request accepted:", task_id, "long:", is_long)
        else
            log.error("sms_handler.tx", "send request failed:", task_id, tostring(ret))
        end
    end)
end

-- ==================== 初始化入口 ====================

function M.init()
    log.info("sms_handler", "initializing")

    -- 开启长短信自动合并(默认即为 true, 显式开启更稳妥)
    pcall(sms.autoLong, true)
    -- 清理可能残留的长短信分片缓存
    local ok_clear, cleared = pcall(sms.clearLong)
    if ok_clear then
        log.info("sms_handler", "cleared long fragments:", tostring(cleared))
    end

    M.init_receiver()
    log.info("sms_handler", "ready")
end

return M
