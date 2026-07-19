--[[
    call_handler.lua - 电话呼叫处理模块
    负责来电/去电事件上报、拨号/挂断动作执行
    依赖: main.lua 暴露的 _G.comm (send_to_host)
]]

local M = {}
local comm = _G.comm  -- main.lua 暴露的串口通信层

-- ==================== 内部状态 ====================
local current_call_state = "idle" -- idle | ringing | connected
local call_phone = ""

-- ==================== 辅助函数 ====================

-- 上报电话状态变化到主机
local function emit_call_event(event_type, phone, extra)
    local event = {
        event = event_type,
        phone = tostring(phone or "")
    }
    
    -- 合并额外字段
    if extra then
        for k, v in pairs(extra) do
            event[k] = v
        end
    end
    
    log.info("call_handler.event", event_type, phone or "")
    comm.log_to_host("info", "call_handler", event_type .. " " .. tostring(phone or ""))
    
    local ok = comm.send_to_host(event)
    if not ok then
        log.error("call_handler", "failed to send call event")
    end
end

-- ==================== 上行事件: 呼叫状态监听 ====================

--[[
    注册所有电话相关的事件订阅
]]
function M.init_call_monitor()
    log.info("call_handler", "registering CC_IND call events")

    local function handle_incoming(phone)
        if current_call_state == "ringing" then return end
        current_call_state = "ringing"
        call_phone = tostring(phone or (cc and cc.lastNum and cc.lastNum()) or "")
        -- 来电: 指示灯临时快闪
        if comm.netled then comm.netled.active(3000) end
        emit_call_event("call_incoming", call_phone)
    end

    local function handle_disconnected(reason)
        local old_state = current_call_state
        local phone = call_phone
        if phone == "" and cc and cc.lastNum then
            phone = tostring(cc.lastNum() or "")
        end
        current_call_state = "idle"
        call_phone = ""

        local disconnect_reason = "hangup"
        local reason_str = tostring(reason or ""):lower()
        if reason_str:find("busy") or reason == 1 or reason == "17" then
            disconnect_reason = "busy"
        elseif (reason_str:find("no") and reason_str:find("answer")) or
            reason == 2 or reason == "18" or reason == 19 or old_state == "ringing" then
            disconnect_reason = "no_answer"
        end

        emit_call_event("call_disconnected", phone, {
            reason = disconnect_reason,
            raw_reason = reason,
        })
    end

    -- LuatOS cc 模块的标准事件入口，号码通过 cc.lastNum() 获取。
    sys.subscribe("CC_IND", function(status, reason)
        local status_text = tostring(status or ""):upper()
        if status_text == "INCOMINGCALL" then
            handle_incoming(cc and cc.lastNum and cc.lastNum() or "")
        elseif status_text == "DISCONNECTED" then
            handle_disconnected(reason)
        elseif status_text == "CONNECTED" or status_text == "ACTIVE" then
            current_call_state = "connected"
            call_phone = tostring((cc and cc.lastNum and cc.lastNum()) or call_phone)
            log.info("call_handler", "connected:", call_phone)
        else
            log.info("call_handler", "cc status:", status_text)
        end
    end)

    log.info("call_handler", "call monitor ready")
end

-- ==================== 下行动作: 拨号/挂断 ====================

--[[
    处理来自主机的 dial 命令
    @param msg: {action="dial", id=uuid, phone=...}
]]
M.handle_dial = function(msg)
    if not msg.phone or msg.phone == "" then
        log.error("call_handler.dial", "missing phone number")
        return
    end
    
    local target = tostring(msg.phone)
    log.info("call_handler.dial", "calling:", target)
    
    sys.taskInit(function()
        local ok, ret = pcall(cc.dial, target)
        
        local accepted = ok and ret ~= false and ret ~= nil
        if accepted then
            log.info("call_handler.dial", "dial initiated:", target, "return:", tostring(ret))
            comm.log_to_host("info", "call_handler.dial", "dial initiated: " .. target)
        else
            log.error("call_handler.dial", "dial failed:", ret)
            comm.log_to_host("error", "call_handler.dial", "dial failed: " .. tostring(ret))
            emit_call_event("call_disconnected", target, {
                reason = "dial_failed",
                error = tostring(ret)
            })
        end
    end)
end

--[[
    处理来自主机的 hangup 命令
    @param msg: {action="hangup"}
]]
M.handle_hangup = function(msg)
    log.info("call_handler.hangup", "hanging up")
    
    sys.taskInit(function()
        local ok, ret = pcall(cc.hangUp)
        
        local accepted = ok and ret ~= false and ret ~= nil
        if accepted then
            log.info("call_handler.hangup", "hangup success, return:", tostring(ret))
        else
            log.error("call_handler.hangup", "hangup failed:", ret)
        end
        
        -- 重置状态
        current_call_state = "idle"
        call_phone = ""
    end)
end

-- ==================== 初始化入口 ====================

function M.init()
    log.info("call_handler", "initializing")
    
    M.init_call_monitor()
    
    log.info("call_handler", "ready")
end

return M
