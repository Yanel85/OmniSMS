--[[
    util_netled.lua - 网络状态指示灯控制模块 (Air780 家族 LuatOS)
    依赖: LuatOS 核心库 sys / pwm / gpio / log

    状态机:
      1. 开机呼吸灯(breathing): 上电即运行, 直到 init()/waiting() 被调用
      2. 在线慢闪(online):     注册网络 或 主机握手(identify) 后 -> 正常节奏闪烁
      3. 等待连接快闪(waiting): 已注册网络但主机尚未握手 -> 持续快闪
      4. 活动快闪(active):      收发短信 / 来电 时临时快闪, 超时自动恢复基础节奏

    引脚(可按硬件修改):
      PWM_ID   : 呼吸灯使用的 PWM 通道(对应 Air780 某 GPIO)
      LED_GPIO : 闪烁使用的 GPIO 引脚(NetLed, 低电平/高电平点亮取决于硬件)
]]

local util_netled = {}

-- ==================== 引脚与节奏配置 ====================
local PWM_ID = 4          -- 呼吸灯 PWM 通道
local LED_GPIO = 27       -- 闪烁 LED GPIO

-- 在线(正常)节奏
local netled_default_duration = 200   -- 点亮时长(ms)
local netled_default_interval = 3000  -- 熄灭间隔(ms)

-- 快闪节奏(等待连接 / 收发短信 / 来电)
local netled_fast_duration = 80
local netled_fast_interval = 120

local netled_duration = netled_default_duration
local netled_interval = netled_default_interval

local netled_inited = false

-- ==================== 开机呼吸灯效果 ====================
-- 上电即运行, 直到被 init()/waiting() 通过 "NET_LED_INIT" 事件中止
sys.taskInit(function()
    local nums = { 0, 1, 2, 4, 6, 12, 16, 21, 27, 34, 42, 51, 61, 72, 85, 100, 100 }
    local len = #nums
    while true do
        for i = 1, len, 1 do
            pwm.open(PWM_ID, 1000, nums[i])
            local result = sys.waitUntil("NET_LED_INIT", 25)
            if result then
                pwm.close(PWM_ID)
                return
            end
        end
        for i = len, 1, -1 do
            pwm.open(PWM_ID, 1000, nums[i])
            local result = sys.waitUntil("NET_LED_INIT", 25)
            if result then
                pwm.close(PWM_ID)
                return
            end
        end
    end
end)

-- ==================== 内部: 启动闪烁任务(仅首次停止呼吸灯) ====================
local function start_blink()
    if netled_inited then return end
    netled_inited = true
    sys.publish("NET_LED_INIT")  -- 通知呼吸灯任务退出

    sys.taskInit(function()
        local netled = gpio.setup(LED_GPIO, 0, gpio.PULLUP)
        while true do
            netled(1)
            sys.waitUntil("NET_LED_UPDATE", netled_duration)
            netled(0)
            sys.waitUntil("NET_LED_UPDATE", netled_interval)
        end
    end)
end

-- ==================== 对外接口 ====================

-- 在线: 正常慢闪(注册网络 或 主机握手后)
function util_netled.init()
    netled_duration = netled_default_duration
    netled_interval = netled_default_interval
    start_blink()
    sys.publish("NET_LED_UPDATE")
end

-- 等待连接: 持续快闪(已注册网络但主机尚未握手)
function util_netled.waiting()
    netled_duration = netled_fast_duration
    netled_interval = netled_fast_interval
    start_blink()
    sys.publish("NET_LED_UPDATE")
end

-- 活动: 临时快闪(收发短信 / 来电), restore 毫秒后自动恢复基础节奏
function util_netled.active(restore)
    if netled_duration == netled_fast_duration and netled_interval == netled_fast_interval then
        -- 已在快闪(等待连接中), 不重复触发
        return
    end
    util_netled.blink(netled_fast_duration, netled_fast_interval, restore)
end

-- 通用: 修改闪烁节奏, 若 restore 给定则在 restore 毫秒后恢复默认节奏
function util_netled.blink(duration, interval, restore)
    if duration == netled_duration and interval == netled_interval then return end
    netled_duration = duration or netled_default_duration
    netled_interval = interval or netled_default_interval
    log.debug("EVENT.NET_LED_UPDATE", duration, interval, restore)
    sys.publish("NET_LED_UPDATE")
    if restore then sys.timerStart(util_netled.blink, restore) end
end

return util_netled
