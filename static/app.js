/**
 * OmniSMS v2.0 - 设备与通讯管理系统
 * 桌面端聊天式布局 + 实时通信
 */

// ==================== 全局状态 ====================
const AppState = {
    devices: new Map(),           // IMEI -> DeviceInfo
    selectedDevice: null,        // 当前选中的标识: 短信/通话模块均为 SIM(手机号/IMSI), 通讯记录按 SIM 归类
    smsMessages: {},            // { simKey: [ 扁平原始短信记录(peer_phone 原样) ] }  simKey = device_id(手机号/IMSI/IMEI兜底), 短信按 SIM 归类
    smsConversations: {},        // { simKey: { normKey: [messages] } }  (按归一化键聚合后的会话消息, 供聊天视图)
    smsConvMeta: {},             // { simKey: [ {peer_phone:normKey,count,last_message,last_direction,last_time} ] }  (号码级聚合, 供左侧列表)
    callRecords: {},             // { simKey: [records] }  (扁平通话记录, 按 SIM 归类)
    callConversations: {},       // { simKey: [ {phone,count,last_type,...} ] }  (号码级聚合, 按 SIM 归类)
    callViewMode: 'aggregated',  // 'aggregated' | 'flat'  (通话列表视图模式)
    callFilterType: 'all',       // 扁平视图下的类型筛选
    selectedCallPhone: null,     // 当前选中的通话号码 (用于高亮)
    logs: [],                    // 日志数组
    filteredLogs: [],            // 过滤后的日志
    ws: null,                     // WebSocket 连接
    
    // 分页状态
    logPage: 1,
    logPageSize: 50,
    
    // 设备备注缓存 (来自后端数据库)
    deviceRemarks: {},
};

// ==================== 初始化 ====================
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initWebSocket();
    loadDevices();
    startPolling();
    syncScanStatus();
});

// ==================== 导航切换 ====================
function initNavigation() {
    const tabs = document.querySelectorAll('.nav-tab');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const module = tab.dataset.module;
            
            // 更新标签激活状态
            tabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            
            // 切换模块显示
            document.querySelectorAll('.module').forEach(m => m.classList.remove('active'));
            const targetModule = document.getElementById(`module-${module}`);
            if (targetModule) targetModule.classList.add('active');
            
            // 模块切换时的初始化
            switch(module) {
                case 'devices': refreshDevices(); break;
                case 'sms': refreshDeviceSelects(); break;
                case 'call': refreshDeviceSelects(); break;
                case 'logs': renderLogs(); break;
            }
        });
    });
}

// ==================== WebSocket 实时通信 ====================
function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/log`;
    
    AppState.ws = new WebSocket(wsUrl);
    
    AppState.ws.onopen = () => {
        updateConnectionStatus(true);
        showToast('已连接到服务器', 'success');
    };
    
    AppState.ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'log') {
                addLogEntry(data.data);
            } else if (data.type === 'device_event') {
                handleDeviceEvent(data.data);
            } else if (data.type === 'sms_event') {
                handleSMSEvent(data.data);
            } else if (data.type === 'call_event') {
                handleCallEvent(data.data);
            }
        } catch (e) {
            console.error('WS message parse error:', e);
        }
    };
    
    AppState.ws.onclose = () => {
        updateConnectionStatus(false);
        showToast('连接已断开', 'warning');
        setTimeout(initWebSocket, 3000); // 自动重连
    };
    
    AppState.ws.onerror = (err) => {
        console.error('WS error:', err);
    };
}

function updateConnectionStatus(connected) {
    const indicator = document.getElementById('connectionStatus');
    const dot = indicator.querySelector('.status-dot');
    const text = indicator.querySelector('.status-text');
    
    dot.className = `status-dot ${connected ? 'online' : 'offline'}`;
    text.textContent = connected ? '在线' : '离线';
}

// ==================== API 调用封装 ====================
async function apiGet(url) {
    try {
        const res = await fetch(url);
        return await res.json();
    } catch (e) {
        console.error('API GET error:', url, e);
        return null;
    }
}

async function apiPost(url, body) {
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        return await res.json();
    } catch (e) {
        console.error('API POST error:', url, e);
        return null;
    }
}

// ==================== 轮询刷新 ====================
function startPolling() {
    setInterval(() => {
        if (document.getElementById('module-devices').classList.contains('active')) {
            refreshDevices();
        }
    }, 10000); // 10秒轮询设备状态
}

async function refreshAll() {
    await Promise.all([refreshDevices(), refreshLogs()]);
    showToast('已刷新全部数据', 'success');
}

// ==================== 设备管理模块 ====================
async function loadDevices() {
    const result = await apiGet('/api/devices');
    if (!result || !result.devices) return;
    
    AppState.devices.clear();
    AppState.deviceRemarks = {};
    result.devices.forEach(device => {
        AppState.devices.set(device.device_id, device);
        if (device.remark) AppState.deviceRemarks[device.device_id] = device.remark;
    });
    
    renderDeviceList();
    updateDeviceStats();
    refreshDeviceSelects();
}

// 根据 rssi(dBm) 生成信号强度指示器组件
// 以 "信号格 + 数值" 形式呈现, 而非 emoji:
//   - 4 根高度递增的竖条, 按百分比填充(支持满格/半格/空格)
//   - 无信号/离线 -> 灰色弱化; 信号 60% 以上 -> 绿色; 其余 -> 橙色
//   - 数值区显示 rssi (如 -56), 无信号时显示 "无信号"/"离线"
function renderSignalCell(device) {
    const isOnline = device.status === 'online';
    const rssi = device.rssi;
    const hasSignal = isOnline
        && (typeof rssi === 'number')
        && rssi > -113 && rssi !== -999;

    let color, valueText, percent, muted = false;
    if (!hasSignal) {
        color = '#94a3b8';
        muted = true;
        valueText = isOnline ? '无信号' : '离线';
        percent = 0;
    } else {
        // dBm 转百分比: -50dBm≈100%, -100dBm≈0%
        percent = Math.max(0, Math.min(100, Math.round(2 * (rssi + 100))));
        color = percent >= 60 ? '#10b981' : '#f59e0b'; // 绿色 / 橙色
        valueText = rssi;
    }

    // 4 根信号格, 高度递增 (px)
    const barHeights = [4, 7, 10, 14];
    let bars = '';
    for (let i = 0; i < barHeights.length; i++) {
        // 当前格填充比例: 0~1 (支持半格)
        const fill = Math.max(0, Math.min(1, (percent / 100 * barHeights.length) - i));
        bars += `<span class="signal-bar" style="height:${barHeights[i]}px;">`
            + `<i style="height:${(fill * 100).toFixed(0)}%"></i></span>`;
    }

    return `<span class="signal-indicator-wrap${muted ? ' is-muted' : ''}" style="color:${color};">`
        + `<span class="signal-indicator" aria-hidden="true">${bars}</span>`
        + `<span class="signal-value">${valueText}</span></span>`;
}

// 根据 IMSI 识别中国运营商 (MCC=460)。无法识别时返回空串(不显示徽章)。
function getCarrierName(imsi) {
    if (!imsi || !/^\d{5,}$/.test(String(imsi).trim())) return '';
    const mcc = String(imsi).trim().slice(0, 3);
    if (mcc !== '460') return ''; // 仅识别中国运营商
    const mnc = String(imsi).trim().slice(3, 5);
    const mobile = ['00', '02', '04', '07', '08'];   // 中国移动
    const unicom = ['01', '06', '09'];               // 中国联通
    const telecom = ['03', '05', '11'];              // 中国电信
    const broadcast = ['15'];                        // 中国广电
    if (mobile.includes(mnc)) return '中国移动';
    if (unicom.includes(mnc)) return '中国联通';
    if (telecom.includes(mnc)) return '中国电信';
    if (broadcast.includes(mnc)) return '中国广电';
    return '未知运营商';
}

// 运营商 -> 徽章配色 class
function carrierClass(name) {
    switch (name) {
        case '中国移动': return 'carrier-mobile';
        case '中国联通': return 'carrier-unicom';
        case '中国电信': return 'carrier-telecom';
        case '中国广电': return 'carrier-broadcast';
        default: return 'carrier-unknown';
    }
}

// 运营商徽章 HTML (显示在信号图标前方); 无 IMSI 或无法识别时不显示。
function deviceCarrierBadge(device) {
    const name = getCarrierName(device.imsi);
    if (!name) return '';
    return `<span class="carrier-badge ${carrierClass(name)}" title="运营商 (基于 IMSI)">${name}</span>`;
}

function renderDeviceList() {
    const tbody = document.getElementById('deviceListBody');
    
    if (AppState.devices.size === 0) {
        tbody.innerHTML = `
            <tr class="empty-row">
                <td colspan="7" class="text-center py-16">
                    <div class="empty-state">
                        <svg class="w-16 h-16 mx-auto mb-4 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
                        <p>暂无设备连接</p>
                        <p class="empty-hint">设备接入后将自动发现，连接 Air780E 模组后点击刷新</p>
                    </div>
                </td>
            </tr>`;
        return;
    }
    
    let html = '';
    AppState.devices.forEach((device, deviceId) => {
        const remark = AppState.deviceRemarks[deviceId] || '';
        const lastActive = device.last_active ? formatTime(new Date(device.last_active)) : '-';
        const deviceLabel = device.phone || device.imei || deviceId;
        const noCardBadge = device.no_card
            ? '<span class="no-card-badge" title="设备已连接但无 SIM 卡, 短信与通话功能不可用">无卡</span>'
            : '';
        
        html += `
            <tr data-device-id="${deviceId}">
                <td>
                    <div class="signal-cell">
                        ${deviceCarrierBadge(device)}
                        <span class="signal-hover-target" data-device-id="${deviceId}" tabindex="0" title="悬停查看设备详情">${renderSignalCell(device)}</span>
                    </div>
                </td>
                <td class="no-cell">
                    <code class="no-phone">${deviceLabel}</code>${noCardBadge}
                    <button class="btn-action btn-danger" onclick="removeDevice('${deviceId}')" title="移除"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg></button>
                </td>
                <td class="model-cell">
                    <span class="model-value">${device.model || '-'}</span>
                </td>
                <td>
                    ${remark 
                        ? `<span class="device-remark">${escapeHtml(remark)}</span>` 
                        : '<span class="no-remark">-- 未设置 --</span>'}
                    <button class="btn-icon-sm" onclick="showRemarkDialog('${deviceId}')" title="编辑备注"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></button>
                </td>
                <td><code>${device.port || '-'}</code></td>
                <td>${lastActive}</td>
                <td class="action-cell">
                    <button class="btn-action btn-goto-sms" onclick="gotoSMS('${deviceId}')" title="发短信"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg></button>
                    <button class="btn-action btn-goto-call" onclick="gotoCall('${deviceId}')" title="打电话"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"/></svg></button>
                </td>
            </tr>`;
    });
    
    tbody.innerHTML = html;
}

// ==================== 设备详情悬浮 Tooltip ====================
// 鼠标悬停信号图标时, 在浮动气泡中集中展示 ICCID / IMSI / CSQ / IMEI / RSRP / RSRQ / SNR。
let deviceTooltipEl = null;

function getDeviceTooltip() {
    if (!deviceTooltipEl) {
        deviceTooltipEl = document.createElement('div');
        deviceTooltipEl.className = 'device-tooltip';
        deviceTooltipEl.style.display = 'none';
        document.body.appendChild(deviceTooltipEl);
    }
    return deviceTooltipEl;
}

function buildDeviceTooltipHTML(device) {
    const row = (label, value, unit = '') => {
        const has = value !== null && value !== undefined && value !== '' && value !== -999;
        const val = has ? escapeHtml(String(value)) + (unit ? ' ' + unit : '') : '--';
        return `<div class="dt-row"><span class="dt-label">${label}</span>`
            + `<span class="dt-value">${val}</span></div>`;
    };
    return `<div class="dt-title">设备详细信息</div>`
        + (device.no_card ? row('SIM 卡', '无卡 (短信/通话不可用)') : '')
        + row('ICCID', device.iccid)
        + row('IMSI', device.imsi)
        + row('IMEI', device.imei)
        + row('CSQ', device.csq)
        + row('RSSI', device.rssi, 'dBm')
        + row('RSRP', device.rsrp, 'dBm')
        + row('RSRQ', device.rsrq, 'dB')
        + row('SNR', device.snr, 'dB');
}

function showDeviceTooltip(target, device) {
    const tip = getDeviceTooltip();
    const id = target.getAttribute('data-device-id');
    if (tip.dataset.deviceId === id) {
        // 已为该设备显示, 仅重新定位避免闪烁
        positionDeviceTooltip(target, tip);
        return;
    }
    tip.dataset.deviceId = id;
    tip.innerHTML = buildDeviceTooltipHTML(device);
    tip.style.display = 'block';
    positionDeviceTooltip(target, tip);
}

function positionDeviceTooltip(target, tip) {
    const rect = target.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - tipRect.width / 2;
    let top = rect.top - tipRect.height - 10;
    left = Math.max(8, Math.min(left, window.innerWidth - tipRect.width - 8));
    if (top < 8) top = rect.bottom + 10; // 上方空间不足则显示在下方
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
}

function hideDeviceTooltip() {
    if (deviceTooltipEl) {
        deviceTooltipEl.style.display = 'none';
        delete deviceTooltipEl.dataset.deviceId;
    }
}

// 事件委托: 设备列表会频繁重渲染, 统一在 document 上监听
document.addEventListener('mouseover', (e) => {
    const target = e.target.closest('.signal-hover-target');
    if (!target) return;
    const device = AppState.devices.get(target.getAttribute('data-device-id'));
    if (!device) return;
    showDeviceTooltip(target, device);
});
document.addEventListener('mouseout', (e) => {
    const target = e.target.closest('.signal-hover-target');
    if (!target) return;
    const related = e.relatedTarget;
    if (related && target.contains(related)) return; // 仍在图标内部
    hideDeviceTooltip();
});
// 键盘可达性: 聚焦时也展示
document.addEventListener('focusin', (e) => {
    const target = e.target.closest('.signal-hover-target');
    if (!target) return;
    const device = AppState.devices.get(target.getAttribute('data-device-id'));
    if (device) showDeviceTooltip(target, device);
});
document.addEventListener('focusout', (e) => {
    const target = e.target.closest('.signal-hover-target');
    if (target) hideDeviceTooltip();
});

function updateDeviceStats() {
    let online = 0, offline = 0;
    AppState.devices.forEach(d => {
        if (d.status === 'online') online++;
        else offline++;
    });
    
    document.getElementById('deviceOnlineCount').textContent = online;
    document.getElementById('deviceOfflineCount').textContent = offline;
    document.getElementById('deviceTotalCount').textContent = AppState.devices.size;
}

async function refreshDevices() {
    await loadDevices();
}

// ==================== 手动扫描控制 ====================
// 说明: 后端改为"每端口独立探测"(每个端口最多等待 N 秒), 扫描总时长随端口数变化,
// 故前端不再使用固定倒计时, 改为轮询 /api/scan/status 检测扫描是否结束。
let scanPollTimer = null;   // 扫描状态轮询定时器
let scanElapsed = 0;        // 已扫描秒数(仅用于按钮展示)

// 同步扫描按钮状态(页面加载或切换回来时)
async function syncScanStatus() {
    const status = await apiGet('/api/scan/status');
    // 手动扫描状态
    if (status && status.scanning) {
        startScanPolling();
    } else {
        resetScanButton();
    }
    // 后台自动扫描状态: 仅当自动扫描运行时显示"停止自动扫描"按钮
    if (status && status.auto_scanning) {
        showStopAutoScanButton();
        startAutoScanPoll();
    } else {
        hideStopAutoScanButton();
        stopAutoScanPoll();
    }
}

// ==================== 后台自动扫描控制 ====================
let autoScanPollTimer = null;   // 自动扫描状态轮询定时器

// 停止后台自动扫描: 立即中断扫描, 隐藏按钮, 恢复界面至初始状态
async function stopAutoScan() {
    const res = await apiPost('/api/scan/auto/stop', {});
    hideStopAutoScanButton();
    stopAutoScanPoll();
    showToast((res && res.message) || '已停止自动扫描', 'info');
    refreshDevices();
}

function showStopAutoScanButton() {
    const btn = document.getElementById('btnStopAutoScan');
    if (btn) btn.style.display = '';
}

function hideStopAutoScanButton() {
    const btn = document.getElementById('btnStopAutoScan');
    if (btn) btn.style.display = 'none';
}

// 轮询后端, 保持"停止自动扫描"按钮与真实状态一致(如被其它客户端停止)
function startAutoScanPoll() {
    if (autoScanPollTimer) clearInterval(autoScanPollTimer);
    autoScanPollTimer = setInterval(async () => {
        const status = await apiGet('/api/scan/status');
        if (!status || !status.auto_scanning) {
            hideStopAutoScanButton();
            stopAutoScanPoll();
        }
    }, 2000);
}

function stopAutoScanPoll() {
    if (autoScanPollTimer) {
        clearInterval(autoScanPollTimer);
        autoScanPollTimer = null;
    }
}

// 切换扫描: 未扫描 -> 开始; 扫描中 -> 停止
async function toggleScan() {
    const status = await apiGet('/api/scan/status');
    const scanning = status && status.scanning;

    if (scanning) {
        const res = await apiPost('/api/scan/stop', {});
        stopScanPolling();
        resetScanButton();
        showToast((res && res.message) || '已停止扫描', 'info');
    } else {
        const res = await apiPost('/api/scan', { duration: 15 });
        if (res && res.success) {
            startScanPolling();
            showToast(res.message || '已开始扫描', 'success');
        } else {
            showToast((res && res.message) || '启动扫描失败', 'error');
        }
    }
}

function startScanPolling() {
    scanElapsed = 0;
    updateScanButton();
    if (scanPollTimer) clearInterval(scanPollTimer);
    scanPollTimer = setInterval(async () => {
        scanElapsed++;
        updateScanButton();
        // 每 2 秒查询一次后端扫描状态
        if (scanElapsed % 2 === 0) {
            const status = await apiGet('/api/scan/status');
            if (!status || !status.scanning) {
                stopScanPolling();
                resetScanButton();
                showToast('扫描结束', 'info');
                refreshDevices();
            }
        }
    }, 1000);
}

function stopScanPolling() {
    if (scanPollTimer) {
        clearInterval(scanPollTimer);
        scanPollTimer = null;
    }
}

function updateScanButton() {
    const btn = document.getElementById('btnScan');
    if (!btn) return;
    btn.textContent = `扫描中… 点击停止 (${scanElapsed}s)`;
    btn.className = 'px-4 py-2 bg-red-500 hover:bg-red-600 text-white rounded-lg text-sm font-medium transition-colors';
    btn.disabled = false;
}

function resetScanButton() {
    const btn = document.getElementById('btnScan');
    if (!btn) return;
    btn.textContent = '扫描设备';
    btn.className = 'px-4 py-2 bg-brand-500 hover:bg-brand-600 text-white rounded-lg text-sm font-medium transition-colors';
}

function closeModal(id) {
    document.getElementById(id).classList.remove('show');
}

// 设备备注功能
function showRemarkDialog(imei) {
    document.getElementById('remarkImei').value = imei;
    document.getElementById('remarkName').value = AppState.deviceRemarks[imei] || '';
    document.getElementById('remarkModal').classList.add('show');
    document.getElementById('remarkName').focus();
}

function saveRemark() {
    const imei = document.getElementById('remarkImei').value;
    const name = document.getElementById('remarkName').value.trim();
    
    // 乐观更新本地缓存
    if (name) {
        AppState.deviceRemarks[imei] = name;
    } else {
        delete AppState.deviceRemarks[imei];
    }
    
    closeModal('remarkModal');
    renderDeviceList(); // 重新渲染表格
    refreshDeviceSelects();
    
    // 持久化到后端数据库
    apiPost('/api/devices/remark', { device_id: imei, remark: name })
        .then(result => {
            if (result && result.success) {
                showToast('备注已保存', 'success');
            } else {
                showToast('备注保存失败', 'error');
            }
        });
}

// 移除设备
async function removeDevice(imei) {
    const remark = AppState.deviceRemarks[imei] || imei.substring(0, 12) + '...';
    if (!confirm(`确定要删除设备 "${remark}" 吗?\n\n该操作会从数据库中彻底删除此设备及其备注，且会断开实际连接。`)) {
        return;
    }
    
    const result = await apiPost('/api/disconnect', { device_id: imei });
    if (result && result.success) {
        delete AppState.deviceRemarks[imei];
        AppState.devices.delete(imei);
        renderDeviceList();
        updateDeviceStats();
        showToast('设备已删除', 'success');
    } else {
        showToast('删除失败', 'error');
    }
}

// 快捷跳转
function gotoSMS(simKey) {
    AppState.selectedDevice = simKey;
    document.querySelector('[data-module="sms"]').click();
    document.getElementById('smsSimSelect').value = simKey;
    onSMSSimChange();
}

function gotoCall(simKey) {
    AppState.selectedDevice = simKey;
    document.querySelector('[data-module="call"]').click();
    document.getElementById('callSimSelect').value = simKey;
    onCallSimChange();
}

// 判断某 SIM/设备是否为无卡状态(既无号码也无 IMSI, device_id 回退 IMEI, 短信/通话不可用)
function isDeviceNoCard(key) {
    const dev = AppState.devices.get(key);
    return !!(dev && dev.no_card);
}

// 刷新设备/SIM 选择下拉框
// 短信与通话模块均按 SIM(手机号/IMSI) 归类, 下拉直接列出 SIM
function refreshDeviceSelects() {
    const smsSelect = document.getElementById('smsSimSelect');
    const callSelect = document.getElementById('callSimSelect');

    let smsOpts = '<option value="">-- 选择 SIM (手机号/IMSI) --</option>';
    let callOpts = '<option value="">-- 选择 SIM (手机号/IMSI) --</option>';

    AppState.devices.forEach((dev, deviceId) => {
        // 以 SIM 标识(手机号 > IMSI > IMEI兜底)作为选项值, 直接选 SIM
        const simLabel = dev.phone || dev.imsi || dev.imei || deviceId;
        const simRemark = AppState.deviceRemarks[deviceId] || '';
        // 无卡状态: 在显示中标注, 但选项仍可选(用于查看, 实际功能后端会拦截)
        const cardTag = dev.no_card ? ' · 无卡' : '';
        const simDisplay = (simRemark ? `${simRemark} (${simLabel})` : simLabel) + cardTag;
        const opt = `<option value="${escapeHtml(deviceId)}">${escapeHtml(`${simDisplay} (${dev.status})`)}</option>`;
        smsOpts += opt;

        // 通话: 同样按 SIM 归类
        callOpts += opt;
    });

    smsSelect.innerHTML = smsOpts;
    callSelect.innerHTML = callOpts;
}

// ==================== 短信模块 (聊天式布局) ====================
let currentConversationPhone = null;

// 监听设备选择变化
document.addEventListener('DOMContentLoaded', () => {
    const smsSelect = document.getElementById('smsSimSelect');
    if (smsSelect) {
        smsSelect.addEventListener('change', onSMSSimChange);
    }
});

async function onSMSSimChange() {
    const simKey = document.getElementById('smsSimSelect').value;
    AppState.selectedDevice = simKey || null;
    currentConversationPhone = null;
    resetSMSSendWindow();
    
    if (!simKey) {
        renderConversationList(null);
        clearChatArea('sms');
        return;
    }
    
    // 短信按 SIM(手机号/IMSI) 归类: 以 simKey(=device_id) 拉取该 SIM 的全部短信(扁平, peer_phone 原样); 聚合与展示格式化在前端完成
    const result = await apiGet(`/api/sms/conversations?device_id=${encodeURIComponent(simKey)}`);
    AppState.smsMessages[simKey] = (result && result.messages) ? result.messages : [];
    aggregateSMSConversations(simKey);
    
    renderConversationList(simKey);
    clearChatArea('sms');
}

// 刷新短信数据 (发送/收到后调用, 保证计数与摘要最新): 重新拉取扁平记录并前端聚合
async function refreshSMSConversations(simKey) {
    if (!simKey) return;
    const result = await apiGet(`/api/sms/conversations?device_id=${encodeURIComponent(simKey)}`);
    AppState.smsMessages[simKey] = (result && result.messages) ? result.messages : [];
    aggregateSMSConversations(simKey);
    renderConversationList(simKey);
}

// 前端号码聚合: 将扁平短信记录按归一化键(normalizePhone)分组为会话
function aggregateSMSConversations(simKey) {
    const flat = AppState.smsMessages[simKey] || [];
    const groups = {};  // normKey -> 聚合对象
    flat.forEach(m => {
        const key = normalizePhone(m.peer_phone);
        if (!groups[key]) {
            groups[key] = { peer_phone: key, messages: [], count: 0, last_time: '', last_message: '', last_direction: 'in' };
        }
        const g = groups[key];
        g.messages.push(m);
        g.count++;
        if (!g.last_time || m.time > g.last_time) {
            g.last_time = m.time;
            g.last_message = m.text;
            g.last_direction = (m.direction === 'out' || m.direction === 'sent') ? 'out' : 'in';
        }
    });
    const list = Object.values(groups).sort((a, b) => (b.last_time || '').localeCompare(a.last_time || ''));
    AppState.smsConvMeta[simKey] = list;
    // 重建按归一化键索引的会话消息(供聊天视图)
    AppState.smsConversations[simKey] = {};
    list.forEach(g => { AppState.smsConversations[simKey][g.peer_phone] = g.messages; });
}

// 渲染会话列表（左侧）：按号码聚合，仅展示最新一条短信 + 摘要
function renderConversationList(simKey) {
    const container = document.getElementById('smsConversationList');
    
    if (!simKey) {
        container.innerHTML = '<div class="empty-conversation py-12 text-center text-white/50 text-sm"><p>请先选择 SIM</p></div>';
        return;
    }
    
    const convos = AppState.smsConvMeta[simKey] || [];
    
    if (convos.length === 0) {
        container.innerHTML = '<div class="empty-conversation py-12 text-center text-white/50 text-sm"><p>暂无会话记录<br><small class="opacity-70">发送新短信开始对话</small></p></div>';
        return;
    }
    
    // 已按服务端 last_time 倒序，确保最新会话在最前
    let html = '';
    convos.forEach(conv => {
        const phone = conv.peer_phone;
        const displayPhone = conv.display_phone || formatPhoneDisplay(phone);
        const isActive = phone === currentConversationPhone;
        const isSent = conv.last_direction === 'out';
        const dirTag = isSent ? '↗' : '↙';
        const countBadge = conv.count > 1 ? `<span class="conv-count">${conv.count}</span>` : '';
        
        html += `
            <div class="conversation-item ${isActive ? 'active' : ''}" data-phone="${escapeHtml(phone)}" onclick="selectConversation('${escapeHtml(phone)}')">
                <div class="conv-row">
                    <div class="conv-phone">${escapeHtml(displayPhone)}</div>
                    <div class="conv-time">${conv.last_time ? formatTimeShort(new Date(conv.last_time)) : ''}</div>
                </div>
                <div class="conv-preview-row">
                    <span class="conv-dir ${isSent ? 'out' : 'in'}">${dirTag}</span>
                    <span class="conv-preview">${escapeHtml(truncate(conv.last_message || '', 28))}</span>
                    ${countBadge}
                </div>
            </div>`;
    });
    
    container.innerHTML = html;
}

// 选择会话
async function selectConversation(phone) {
    currentConversationPhone = phone;
    const displayPhone = formatPhoneDisplay(phone);
    document.getElementById('smsTargetPhone').value = displayPhone;
    document.getElementById('btnClearSmsHistory').disabled = false;
    
    // 将发送窗口绑定到该号码：隐藏号码输入框，显示只读标签(带国家码标准格式)
    document.getElementById('smsPhoneRow').style.display = 'none';
    document.getElementById('smsTargetLabel').textContent = displayPhone;
    document.getElementById('smsTargetLabel').title = displayPhone;
    
    // 高亮选中项
    document.querySelectorAll('.conversation-item').forEach(item => {
        item.classList.toggle('active', item.dataset.phone === phone);
    });
    
    // 使用前端已聚合的会话消息(后端按原始号码存储, 无需二次请求)
    // 聚合已在 aggregateSMSConversations 中完成, 此处直接渲染
    renderChatMessages(phone);
    updateChatHeader(phone);
}

// 渲染聊天消息（右侧）
function renderChatMessages(phone) {
    const area = document.getElementById('smsMessagesArea');
    const simKey = AppState.selectedDevice;
    
    if (!simKey || !phone) {
        area.innerHTML = `
            <div class="chat-placeholder h-full flex flex-col items-center justify-center text-slate-400">
                <svg class="placeholder-icon w-20 h-20 mb-4 opacity-40" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>
                <p>从左侧选择联系人开始对话</p>
                <p class="placeholder-hint text-slate-300 text-sm mt-1">或使用下方输入框发送新短信</p>
            </div>`;
        return;
    }
    
    const messages = AppState.smsConversations[simKey]?.[phone] || [];
    
    if (messages.length === 0) {
        area.innerHTML = `
            <div class="chat-placeholder h-full flex flex-col items-center justify-center text-slate-400">
                <p>暂无消息记录</p>
                <p class="placeholder-hint text-slate-300 text-sm mt-1">发送第一条短信</p>
            </div>`;
        return;
    }
    
    let html = '';
    let lastDate = null;
    
    messages.forEach(msg => {
        // 日期分隔线
        const msgDate = new Date(msg.time).toDateString();
        if (msgDate !== lastDate) {
            lastDate = msgDate;
            html += `<div class="date-separator"><span>${formatDate(new Date(msg.time))}</span></div>`;
        }
        
        const isSent = msg.direction === 'sent';
        const senderName = isSent ? '我' : formatPhoneDisplay(phone);
        const avatar = isSent ? '我' : (String(phone).slice(-4) || '?');
        const statusMark = isSent ? (msg.status === 'delivered' ? '✓✓' :
            msg.status === 'failed' ? '✕' : msg.status === 'pending' ? '…' : '✓') : '';
        
        html += `
            <div class="message-row ${isSent ? 'sent' : 'received'}">
                <div class="msg-avatar ${isSent ? 'me' : ''}" title="${senderName}">${escapeHtml(avatar)}</div>
                <div class="msg-col">
                    <div class="msg-sender">${escapeHtml(senderName)}</div>
                    <div class="chat-bubble">
                        <div class="bubble-content">${escapeHtml(msg.text)}</div>
                        <div class="bubble-time">${formatTimeShort(new Date(msg.time))}${statusMark ? ` <span class="bubble-status">${statusMark}</span>` : ''}</div>
                    </div>
                </div>
            </div>`;
    });
    
    area.innerHTML = html;
    scrollToBottom(area);
}

// 更新聊天头部
function updateChatHeader(phone) {
    const header = document.getElementById('smsChatHeader');
    if (!header) return;
    const target = header.querySelector('.contact-phone') || header;
    target.textContent = phone ? formatPhoneDisplay(phone) : '新对话';
}

// 重置发送窗口：显示号码输入框，清空绑定标签
function resetSMSSendWindow() {
    const phoneRow = document.getElementById('smsPhoneRow');
    const phoneInput = document.getElementById('smsTargetPhone');
    const label = document.getElementById('smsTargetLabel');
    if (phoneRow) phoneRow.style.display = 'flex';
    if (phoneInput) phoneInput.value = '';
    if (label) {
        label.textContent = '未选择';
        label.removeAttribute('title');
    }
}

// 开始新对话：清空当前会话并允许输入新号码
function startNewSMSConversation() {
    currentConversationPhone = null;
    document.getElementById('smsTargetPhone').value = '';
    document.getElementById('btnClearSmsHistory').disabled = true;
    document.getElementById('smsPhoneRow').style.display = 'flex';
    document.getElementById('smsTargetLabel').textContent = '未选择';
    document.getElementById('smsTargetLabel').removeAttribute('title');
    document.getElementById('smsTargetPhone').focus();
    renderChatMessages(null);
    updateChatHeader(null);
}

// 发送短信
async function sendSMS() {
    const simKey = AppState.selectedDevice;
    const phone = document.getElementById('smsTargetPhone').value.trim();
    const text = document.getElementById('smsMessageText').value.trim();
    
    if (!simKey) {
        showToast('请先选择 SIM', 'error');
        return;
    }
    if (isDeviceNoCard(simKey)) {
        showToast('设备无卡，短信功能不可用', 'error');
        return;
    }
    if (!phone) {
        showToast('请输入目标号码', 'error');
        document.getElementById('smsTargetPhone').focus();
        return;
    }
    if (!text) {
        showToast('请输入短信内容', 'error');
        document.getElementById('smsMessageText').focus();
        return;
    }
    
    const btn = document.getElementById('btnSendSMS');
    btn.disabled = true;
    btn.textContent = '发送中...';
    
    const result = await apiPost('/api/sms/send', { device_id: simKey, phone, text });
    
    btn.disabled = false;
    btn.innerHTML = '发送短信 <svg class="w-4 h-4 inline ml-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg>';
    
    if (result && result.success && result.data?.task_id) {
        const taskId = result.data.task_id;
        const normPhone = normalizePhone(phone);
        // 原始号码入扁平存储, 会话聚合与展示格式化在前端完成
        addSMSToHistory(simKey, phone, text, 'sent', 'pending', taskId);
        document.getElementById('smsMessageText').value = '';
        updateCharCount();
        
        showToast('短信发送请求已提交，等待设备回执', 'info');
        
        // 如果当前正在查看这个会话，更新显示
        if (currentConversationPhone === normPhone) {
            renderChatMessages(normPhone);
        }
        refreshSMSConversations(simKey);
    } else {
        showToast(result?.detail || result?.message || '发送失败', 'error');
    }
}

// 添加短信到本地扁平历史(乐观更新), 随后触发前端聚合
function addSMSToHistory(simKey, rawPhone, text, direction, status = 'delivered', taskId = null, time = null) {
    if (!AppState.smsMessages[simKey]) {
        AppState.smsMessages[simKey] = [];
    }
    AppState.smsMessages[simKey].push({
        // 优先使用后端统一下发的 UTC 时间戳(已含时区); 无则回退浏览器本地时钟
        peer_phone: rawPhone,
        time: time || new Date().toISOString(),
        text,
        direction,
        status,
        task_id: taskId
    });
    // 重新聚合(乐观更新), 随后 refreshSMSConversations 会从后端重新拉取并覆盖
    aggregateSMSConversations(simKey);
}

// 处理收到的短信事件
function handleSMSEvent(data) {
    if (data.event === 'sms_received') {
        const simKey = data.device_id || AppState.selectedDevice;
        if (simKey && data.phone && data.text) {
            // 原始号码入扁平存储, 会话聚合与展示格式化在前端完成
            const normPhone = normalizePhone(data.phone);
            addSMSToHistory(simKey, data.phone, data.text, 'received', 'received', null, data.time);
            
            // 如果当前在查看该会话，立即更新
            if (currentConversationPhone === normPhone) {
                renderChatMessages(normPhone);
            }
            refreshSMSConversations(simKey);
            
            showToast(`收到来自 ${formatPhoneDisplay(data.phone)} 的短信`, 'info');
        }
    } else if (data.event === 'sms_sent_result') {
        const simKey = data.device_id || AppState.selectedDevice;
        const status = data.status === 'accepted' ? 'pending' :
                       data.status === 'fail' ? 'failed' : data.status;
        const messages = AppState.smsMessages[simKey] || [];
        let updatedPhone = null;
        messages.forEach(m => {
            if (m.task_id === data.id) {
                m.status = status;
                updatedPhone = m.peer_phone;
            }
        });
        if (updatedPhone) {
            aggregateSMSConversations(simKey);
            if (currentConversationPhone === normalizePhone(updatedPhone)) {
                renderChatMessages(currentConversationPhone);
            }
        }
        refreshSMSConversations(simKey);
        if (status === 'failed') {
            const reason = data.reason || `错误码: ${data.error_code ?? '未知'}`;
            showToast(`短信发送失败（${reason}）`, 'error');
        } else if (status === 'pending') {
            showToast('设备已接受短信发送请求，等待运营商处理', 'info');
        }
    }
}

// 清空当前会话历史
async function clearCurrentSMSHistory() {
    const simKey = AppState.selectedDevice;
    const phone = currentConversationPhone;
    
    if (!simKey || !phone) return;
    
    if (!confirm('确定要彻底清空与该号码的所有短信记录吗？此操作不可恢复')) return;

    const res = await apiPost('/api/sms/purge', {
        device_id: simKey,
        phone: phone,
        confirm: true,
    });

    if (!res) {
        showToast('清空失败：无法连接服务器', 'error');
        return;
    }

    if (res.success) {
        if (AppState.smsConversations[simKey]) {
            delete AppState.smsConversations[simKey][phone];
        }
        renderChatMessages(phone);
        await refreshSMSConversations(simKey);
        showToast(res.message || '短信记录已彻底清空', 'success');
    } else {
        showToast(res.message || '清空失败', 'error');
    }
}

// 字数统计
function updateCharCount() {
    const len = document.getElementById('smsMessageText').value.length;
    document.getElementById('smsCharCount').textContent = len;
}

function onPhoneFocus(input) {
    // 如果选择了某个会话，不覆盖
    if (currentConversationPhone) return;
}

// 搜索过滤会话
function filterConversations() {
    const keyword = document.getElementById('smsSearchInput').value.toLowerCase();
    const items = document.querySelectorAll('.conversation-item');
    
    items.forEach(item => {
        const text = item.textContent.toLowerCase();
        item.style.display = text.includes(keyword) ? '' : 'none';
    });
}

// 清空聊天区域
function clearChatArea(module) {
    const area = document.getElementById(`${module}MessagesArea`);
    if (module === 'sms') {
        area.innerHTML = `
            <div class="chat-placeholder h-full flex flex-col items-center justify-center text-slate-400">
                <svg class="placeholder-icon w-20 h-20 mb-4 opacity-40" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>
                <p>从左侧选择联系人开始对话</p>
                <p class="placeholder-hint text-slate-300 text-sm mt-1">或使用下方输入框发送新短信</p>
            </div>`;
    }
}

// ==================== 通话模块 (聊天式布局) ====================
document.addEventListener('DOMContentLoaded', () => {
    const callSelect = document.getElementById('callSimSelect');
    if (callSelect) {
        callSelect.addEventListener('change', onCallSimChange);
    }
    
    // 通话筛选标签
    document.querySelectorAll('.call-filter-tabs .filter-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.call-filter-tabs .filter-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            // “全部”标签使用号码级聚合视图, 其余标签使用扁平筛选视图
            AppState.callViewMode = (tab.dataset.type === 'all') ? 'aggregated' : 'flat';
            AppState.callFilterType = tab.dataset.type;
            renderCallRecordList(AppState.selectedDevice);
        });
    });
});

async function onCallSimChange() {
    const simKey = document.getElementById('callSimSelect').value;
    AppState.selectedDevice = simKey || null;
    
    // 重置通话详情头部与高亮状态
    document.getElementById('callDetailHeader').textContent = '通话控制中心';
    document.querySelectorAll('.call-record-item.active').forEach(i => i.classList.remove('active'));
    AppState.selectedCallPhone = null;
    
    if (simKey) {
        // 通话按 SIM(手机号/IMSI) 归类: 以 simKey(=device_id) 拉取该 SIM 的通话记录(扁平, peer_phone 原样); 聚合与展示格式化在前端完成
        const flatRes = await apiGet(`/api/calls?device_id=${encodeURIComponent(simKey)}`);
        AppState.callRecords[simKey] = (flatRes && flatRes.records) ? flatRes.records : [];
        aggregateCallConversations(simKey);
    } else {
        AppState.callRecords = {};
        AppState.callConversations = {};
    }
    
    renderCallRecordList(simKey);
}

// 刷新通话数据 (拨号/来电后调用, 保持聚合与扁平数据一致)
async function refreshCallData(simKey) {
    if (!simKey) return;
    const flatRes = await apiGet(`/api/calls?device_id=${encodeURIComponent(simKey)}`);
    AppState.callRecords[simKey] = (flatRes && flatRes.records) ? flatRes.records : [];
    aggregateCallConversations(simKey);
    renderCallRecordList(simKey);
}

// 渲染通话记录列表（左侧）
// 视图模式: 'aggregated' (按号码聚合, 仅最新一条) | 'flat' (扁平, 按类型筛选)
// 前端号码聚合: 将扁平通话记录按归一化键(normalizePhone)分组为会话
function aggregateCallConversations(simKey) {
    const flat = AppState.callRecords[simKey] || [];
    const groups = {};  // normKey -> 聚合对象
    flat.forEach(r => {
        const key = normalizePhone(r.peer_phone);
        if (!groups[key]) {
            groups[key] = { peer_phone: key, count: 0, last_time: '', last_type: 'incoming', last_status: '', last_duration: 0 };
        }
        const g = groups[key];
        g.count++;
        if (!g.last_time || r.time > g.last_time) {
            g.last_time = r.time;
            g.last_type = r.type;
            g.last_status = r.status;
            g.last_duration = r.duration || 0;
        }
    });
    const list = Object.values(groups).sort((a, b) => (b.last_time || '').localeCompare(a.last_time || ''));
    AppState.callConversations[simKey] = list;
}

function renderCallRecordList(simKey) {
    const container = document.getElementById('callRecordList');
    
    if (!simKey) {
        container.innerHTML = '<div class="empty-conversation py-12 text-center text-white/50 text-sm"><p>请先选择 SIM</p></div>';
        return;
    }
    
    const mode = AppState.callViewMode || 'aggregated';
    
    // ===== 聚合视图：按号码折叠 =====
    if (mode === 'aggregated') {
        const convos = AppState.callConversations[simKey] || [];
        if (convos.length === 0) {
            container.innerHTML = '<div class="empty-conversation py-12 text-center text-white/50 text-sm"><p>暂无通话记录</p></div>';
            return;
        }
        let html = '';
        convos.forEach(conv => {
            const phone = conv.peer_phone || '未知号码';
            const displayPhone = conv.display_phone || formatPhoneDisplay(phone);
            const typeClass = conv.last_type === 'incoming' ? 'incoming' :
                              conv.last_type === 'outgoing' ? 'outgoing' : 'missed';
            const typeIcon = callTypeIcon(conv.last_type);
            const statusText = conv.last_type === 'missed' ? '未接通'
                             : (conv.last_duration ? `${conv.last_duration}秒` : (conv.last_status || '已通话'));
            const countBadge = conv.count > 1 ? `<span class="call-count">${conv.count}</span>` : '';
            const isActive = phone === AppState.selectedCallPhone;
            
            html += `
                <div class="call-record-item call-conv-item ${typeClass} ${isActive ? 'active' : ''}" data-phone="${escapeHtml(phone)}" onclick="showCallConversation('${escapeHtml(phone)}')">
                    <div class="call-type-icon">${typeIcon}</div>
                    <div class="call-info">
                        <div class="call-phone">${escapeHtml(displayPhone)}</div>
                        <div class="call-meta">${statusText}</div>
                    </div>
                    <div class="call-right">
                        ${countBadge}
                        <div class="call-time">${conv.last_time ? formatTimeShort(new Date(conv.last_time)) : ''}</div>
                    </div>
                </div>`;
        });
        container.innerHTML = html;
        return;
    }
    
    // ===== 扁平视图：按类型筛选 =====
    const filterType = AppState.callFilterType || 'all';
    let records = AppState.callRecords[simKey] || [];
    if (filterType !== 'all') {
        records = records.filter(r => r.type === filterType);
    }
    
    if (records.length === 0) {
        container.innerHTML = '<div class="empty-conversation py-12 text-center text-white/50 text-sm"><p>暂无通话记录</p></div>';
        return;
    }
    
    let html = '';
    records.slice().reverse().forEach(record => {
        const typeClass = record.type === 'incoming' ? 'incoming' : 
                          record.type === 'outgoing' ? 'outgoing' : 'missed';
        const typeIcon = callTypeIcon(record.type);
        const duration = record.duration ? `${record.duration}秒` : '';
        
        html += `
            <div class="call-record-item ${typeClass}" data-phone="${escapeHtml(normalizePhone(record.peer_phone || '未知号码'))}" onclick="showCallDetail('${record.id}')">
                <div class="call-type-icon">${typeIcon}</div>
                <div class="call-info">
                    <div class="call-phone">${escapeHtml(formatPhoneDisplay(record.peer_phone) || '未知号码')}</div>
                    <div class="call-meta">${duration || (record.type === 'missed' ? '未接通' : '')}</div>
                </div>
                <div class="call-time">${formatTimeShort(new Date(record.time))}</div>
            </div>`;
    });
    
    container.innerHTML = html;
}

// 通话类型图标
function callTypeIcon(type) {
    if (type === 'incoming') {
        return '<svg class="w-[18px] h-[18px] text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"/></svg>';
    }
    if (type === 'outgoing') {
        return '<svg class="w-[18px] h-[18px] text-violet-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg>';
    }
    return '<svg class="w-[18px] h-[18px] text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 8l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2M5 3a2 2 0 00-2 2v1c0 8.284 6.716 15 15 15h1a2 2 0 002-2v-3.28a1 1 0 00-.684-.948l-4.493-1.498a1 1 0 00-1.21.502l-1.13 2.257a11.042 11.042 0 01-5.516-5.517l2.257-1.128a1 1 0 00.502-1.21L9.228 3.683A1 1 0 008.279 3H5z"/></svg>';
}

// 显示通话详情（右侧）：按记录 id 定位号码后展示该号码全部历史
function showCallDetail(recordId) {
    const simKey = AppState.selectedDevice;
    const records = AppState.callRecords[simKey] || [];
    const record = records.find(r => r.id === recordId);
    if (!record) return;
    // 以归一化键定位会话, 兼容同一联系人的多种原始号码形态
    showCallConversation(normalizePhone(record.peer_phone) || '未知号码');
}

// 显示某号码的完整通话历史（右侧）
function showCallConversation(phone) {
    const simKey = AppState.selectedDevice;
    const records = AppState.callRecords[simKey] || [];
    phone = phone || '未知号码';

    AppState.selectedCallPhone = phone;

    // 高亮左侧同号码的记录（聚合项与扁平项均生效, 均按归一化键比较）
    document.querySelectorAll('.call-record-item').forEach(item => {
        item.classList.toggle('active', normalizePhone(item.dataset.phone) === phone);
    });

    // 更新头部标题(带国家码标准格式, 由前端格式化)
    document.getElementById('callDetailHeader').textContent = `通话详情 - ${formatPhoneDisplay(phone)}`;

    // 取出该号码的全部记录（来电 / 去电 / 未接）, 按归一化键匹配, 按时间倒序排列
    const phoneRecords = records
        .filter(r => normalizePhone(r.peer_phone || '未知号码') === phone)
        .sort((a, b) => new Date(b.time) - new Date(a.time));

    const area = document.getElementById('callRecordsArea');
    const typeLabels = { incoming: '来电', outgoing: '去电', missed: '未接来电' };

    if (phoneRecords.length === 0) {
        area.innerHTML = `
            <div class="chat-placeholder h-full flex flex-col items-center justify-center text-slate-400">
                <p>暂无该号码的通话记录</p>
            </div>`;
        return;
    }

    let html = `
        <div class="call-detail-summary mb-4 p-4 bg-white rounded-xl border border-slate-200 shadow-sm flex items-center justify-between gap-3">
            <div class="min-w-0">
                <div class="text-lg font-bold text-slate-800 truncate">${escapeHtml(formatPhoneDisplay(phone))}</div>
                <div class="text-sm text-slate-500 mt-1">共 ${phoneRecords.length} 条通话记录（来电 / 去电 / 未接）</div>
            </div>
            <div class="flex gap-2 shrink-0">
                <button class="px-3 py-1.5 bg-emerald-500 hover:bg-emerald-600 text-white rounded-lg text-sm font-medium transition-colors" onclick="callback('${escapeHtml(phone)}')">回拨</button>
                <button class="px-3 py-1.5 bg-brand-500 hover:bg-brand-600 text-white rounded-lg text-sm font-medium transition-colors" onclick="sendSMSCall('${escapeHtml(phone)}')">发短信</button>
            </div>
        </div>`;

    phoneRecords.forEach(r => {
        const type = r.type || 'incoming';
        html += `
            <div class="call-detail-card" style="max-width:none;margin:0 0 12px;border-radius:12px;">
                <div class="detail-header ${type}" style="border-radius:12px 12px 0 0;">
                    <span class="detail-type">${typeLabels[type]}</span>
                    <span class="detail-phone">${escapeHtml(formatDateTime(new Date(r.time)))}</span>
                </div>
                <div class="detail-body" style="padding:14px 16px;">
                    <div class="detail-row"><label>类型:</label><span>${typeLabels[type]}</span></div>
                    <div class="detail-row"><label>时长:</label><span>${r.duration ? r.duration + '秒' : '-'}</span></div>
                    <div class="detail-row"><label>状态:</label><span>${escapeHtml(r.status || '已完成')}</span></div>
                </div>
            </div>`;
    });

    area.innerHTML = html;
}

// 过滤通话记录（兼容旧调用，现已由 renderCallRecordList 的视图模式取代）
function filterCallRecords(type) {
    const items = document.querySelectorAll('.call-record-item');
    
    items.forEach(item => {
        if (type === 'all') {
            item.style.display = '';
        } else {
            item.style.display = item.classList.contains(type) ? '' : 'none';
        }
    });
}

// 拨号盘
function dialKey(key) {
    const display = document.getElementById('dialDisplay');
    display.value += key;
    updateDialDeleteState();
}

// 删除拨号框最后一个字符，并与数字按键联动
function deleteDialChar() {
    const display = document.getElementById('dialDisplay');
    if (!display.value) return;
    display.value = display.value.slice(0, -1);
    updateDialDeleteState();
}

// 根据拨号框内容同步删除按钮的可用状态
function updateDialDeleteState() {
    const display = document.getElementById('dialDisplay');
    const btn = document.getElementById('dialDeleteBtn');
    if (btn) btn.disabled = !display.value;
}

function makeCall() {
    const simKey = AppState.selectedDevice;
    const phone = document.getElementById('dialDisplay').value.trim();
    
    if (!simKey) {
        showToast('请先选择 SIM', 'error');
        return;
    }
    if (isDeviceNoCard(simKey)) {
        showToast('设备无卡，通话功能不可用', 'error');
        return;
    }
    if (!phone) {
        showToast('请输入电话号码', 'error');
        return;
    }
    
    dial(simKey, phone);
}

async function dial(simKey, phone) {
    const result = await apiPost('/api/call/dial', { device_id: simKey, phone });
    
    if (result && result.task_id) {
        // 添加到通话记录
        addCallRecord(simKey, phone, 'outgoing');
        document.getElementById('dialDisplay').value = '';
        updateDialDeleteState();
        showToast(`正在拨打 ${phone}`, 'info');
        refreshCallData(simKey);
    } else {
        showToast(result?.detail || result?.message || '拨号失败', 'error');
    }
}

async function hangupCall() {
    const simKey = AppState.selectedDevice;
    
    if (!simKey) {
        showToast('请先选择 SIM', 'error');
        return;
    }
    if (isDeviceNoCard(simKey)) {
        showToast('设备无卡，通话功能不可用', 'error');
        return;
    }
    
    const result = await apiPost('/api/call/hangup', { device_id: simKey });
    
    if (result && result.success) {
        showToast('已挂断', 'success');
    } else {
        showToast(result?.detail || result?.message || '挂断失败', 'error');
    }
}

function callback(phone) {
    document.getElementById('dialDisplay').value = phone;
    updateDialDeleteState();
    makeCall();
}

function sendSMSCall(phone) {
    // 跳转到短信模块并预填号码(带国家码标准格式)
    const displayPhone = formatPhoneDisplay(phone);
    gotoSMS(AppState.selectedDevice);
    setTimeout(() => {
        document.getElementById('smsTargetPhone').value = displayPhone;
        document.getElementById('smsTargetLabel').textContent = displayPhone;
        document.getElementById('smsTargetLabel').title = displayPhone;
        document.getElementById('smsPhoneRow').style.display = 'none';
    }, 200);
}

// 添加通话记录
function addCallRecord(simKey, phone, type, duration = null) {
    if (!AppState.callRecords[simKey]) {
        AppState.callRecords[simKey] = [];
    }
    
    AppState.callRecords[simKey].push({
        id: `call_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
        time: new Date().toISOString(),
        phone,
        type,
        duration,
        status: type === 'missed' ? '未接通' : '已完成'
    });
}

// 处理来电事件
function handleCallEvent(data) {
    const simKey = data.device_id || AppState.selectedDevice;
    
    if (data.event === 'call_incoming') {
        addCallRecord(simKey, data.phone || data.from, 'incoming');
        showToast(`来电: ${formatPhoneDisplay(data.phone || data.from)}`, 'warning');
    } else if (data.event === 'call_disconnected') {
        addCallRecord(simKey, data.phone, 'outgoing', data.duration);
    }
    
    if (AppState.selectedDevice === simKey) {
        refreshCallData(simKey);
    }
}

// ==================== 日志模块 ====================
function addLogEntry(logData) {
    AppState.logs.unshift(logData);
    
    // 限制最大条数
    if (AppState.logs.length > 5000) {
        AppState.logs = AppState.logs.slice(0, 5000);
    }
    
    applyLogFilter();
    
    if (document.getElementById('autoScrollLogs')?.checked) {
        const container = document.getElementById('logsContainer');
        scrollToBottom(container);
    }
}

function applyLogFilter() {
    const keyword = document.getElementById('logSearchInput')?.value.toLowerCase() || '';
    const level = document.getElementById('logLevelFilter')?.value || '';
    
    AppState.filteredLogs = AppState.logs.filter(log => {
        if (level && log.level !== level) return false;
        if (keyword) {
            const searchStr = `${log.message} ${log.logger} ${log.module}`.toLowerCase();
            if (!searchStr.includes(keyword)) return false;
        }
        return true;
    });
    
    AppState.logPage = 1;
    renderLogs();
}

function filterLogs() {
    applyLogFilter();
}

function renderLogs() {
    const container = document.getElementById('logsContainer');
    const total = AppState.filteredLogs.length;
    
    document.getElementById('logTotalCount').textContent = total;
    
    if (total === 0) {
        container.innerHTML = '<div class="log-empty"><p>暂无日志数据</p></div>';
        document.getElementById('logShowingCount').textContent = '0';
        return;
    }
    
    const start = (AppState.logPage - 1) * AppState.logPageSize;
    const end = Math.min(start + AppState.logPageSize, total);
    const pageLogs = AppState.filteredLogs.slice(start, end);
    
    document.getElementById('logShowingCount').textContent = end - start;
    
    let html = '';
    pageLogs.forEach(log => {
        const levelClass = log.level.toLowerCase();
        html += `
            <div class="log-entry level-${levelClass}">
                <span class="log-timestamp">${formatLogTimestamp(log.timestamp)}</span>
                <span class="log-level ${levelClass}">${log.level.padEnd(7)}</span>
                <span class="log-module">[${log.module}]</span>
                <span class="log-message">${escapeHtml(log.message)}</span>
            </div>`;
    });
    
    container.innerHTML = html;
    renderPagination();
    
    if (document.getElementById('autoScrollLogs')?.checked) {
        scrollToBottom(container);
    }
}

function renderPagination() {
    const total = AppState.filteredLogs.length;
    const totalPages = Math.ceil(total / AppState.logPageSize);
    
    document.getElementById('btnPrevLogPage').disabled = AppState.logPage <= 1;
    document.getElementById('btnNextLogPage').disabled = AppState.logPage >= totalPages;
    
    const pageInfo = `第 ${AppState.logPage} / ${totalPages || 1} 页`;
    document.getElementById('logPageNumbers').innerHTML = `<span>${pageInfo}</span>`;
}

function prevLogPage() {
    if (AppState.logPage > 1) {
        AppState.logPage--;
        renderLogs();
    }
}

function nextLogPage() {
    const totalPages = Math.ceil(AppState.filteredLogs.length / AppState.logPageSize);
    if (AppState.logPage < totalPages) {
        AppState.logPage++;
        renderLogs();
    }
}

function toggleAutoScroll() {
    const checked = document.getElementById('autoScrollLogs').checked;
    if (checked) {
        scrollToBottom(document.getElementById('logsContainer'));
    }
}

async function refreshLogs() {
    // 从数据库加载历史日志
    const result = await apiGet('/api/logs/cache');
    if (result && result.logs) {
        AppState.logs = result.logs;
    } else {
        AppState.logs = [];
    }
    applyLogFilter();
    renderLogs();
}

function clearLogs() {
    if (!confirm('确定要清空所有日志吗？')) return;
    AppState.logs = [];
    AppState.filteredLogs = [];
    renderLogs();
    showToast('日志已清空', 'success');
}

// ==================== 设备事件处理 ====================
function handleDeviceEvent(data) {
    if (data.event === 'boot' || data.event === 'device_registered') {
        // 新设备上线
        refreshDevices();
        showToast(`设备 ${data.device_id} 已上线`, 'success');
    } else if (data.event === 'keepalive') {
        // 心跳，更新最后活跃时间与信号强度
        const device = AppState.devices.get(data.device_id);
        if (device) {
            device.last_active = new Date().toISOString();
            device.status = 'online';
            if (typeof data.rssi === 'number') device.rssi = data.rssi;
        }
    } else if (data.event === 'disconnect') {
        // 设备断开
        const device = AppState.devices.get(data.device_id);
        if (device) {
            device.status = 'offline';
        }
        renderDeviceList();
        updateDeviceStats();
        showToast(`设备 ${data.device_id} 已离线`, 'warning');
    }
}

// ==================== 工具函数 ====================
// 号码标准化(与后端 database.py normalize_phone 规则一致):
// 生成会话聚合匹配键, 剥离国家码 86 与分隔符, 使同一号码无论是否带 +86 都归为同一会话。
function normalizePhone(raw) {
    if (!raw) return raw || '';
    let s = String(raw).trim().replace(/[\s\-\(\)\.]/g, '');
    const explicitIntl = s.startsWith('+') || s.startsWith('00');
    if (s.startsWith('+')) s = s.slice(1);
    else if (s.startsWith('00')) s = s.slice(2);
    const digits = s.replace(/\D/g, '');
    if (!digits) return String(raw).trim();
    if (digits.startsWith('86')) {
        const rest = digits.slice(2);
        if (explicitIntl && rest) return rest;
        if (digits.length === 13 && rest.startsWith('1')) return rest;
    }
    return digits;
}

// 号码展示格式化(与后端 format_phone_display 一致): 中国大陆手机号统一加 +86 前缀。
function formatPhoneDisplay(raw) {
    const key = normalizePhone(raw);
    if (key && /^\d+$/.test(key) && key.length === 11 && key.startsWith('1')) {
        return '+86' + key;
    }
    return key;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function truncate(text, length) {
    if (!text) return '';
    return text.length > length ? text.substring(0, length) + '...' : text;
}

function formatDate(date) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const targetDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    
    const diffDays = Math.floor((today - targetDay) / (1000 * 60 * 60 * 24));
    
    if (diffDays === 0) return '今天';
    if (diffDays === 1) return '昨天';
    if (diffDays < 7) return `${diffDays}天前`;
    
    return `${date.getMonth() + 1}/${date.getDate()}`;
}

function formatTime(date) {
    if (!(date instanceof Date) || isNaN(date)) return '-';
    return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

function formatTimeShort(date) {
    if (!(date instanceof Date) || isNaN(date)) return '';
    return date.toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit'
    });
}

function formatDateTime(date) {
    if (!(date instanceof Date) || isNaN(date)) return '-';
    return date.toLocaleString('zh-CN');
}

// 日志时间戳: 后端统一下发带时区的 UTC ISO 8601, 此处集中换算为浏览器本地时区显示;
// 旧版磁盘日志(无时区字符串)无法可靠换算, 原样展示以避免误判。
function formatLogTimestamp(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (!isNaN(d)) {
        return d.toLocaleString('zh-CN', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
    }
    return ts;
}

function scrollToBottom(element) {
    requestAnimationFrame(() => {
        element.scrollTop = element.scrollHeight;
    });
}

// SVG 图标定义 (替代 emoji)
const ICONS = {
    success: '<svg class="w-[18px] h-[18px] text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>',
    error: '<svg class="w-[18px] h-[18px] text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M6 18L18 6M6 6l12 12"/></svg>',
    warning: '<svg class="w-[18px] h-[18px] text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>',
    info: '<svg class="w-[18px] h-[18px] text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>'
};

// Toast 通知 (使用 SVG 图标)
function showToast(message, type = 'info', duration = 3000) {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        <span class="toast-icon">${ICONS[type] || ICONS.info}</span>
        <span class="toast-message">${message}</span>
    `;
    
    container.appendChild(toast);
    
    // 触发动画
    requestAnimationFrame(() => toast.classList.add('show'));
    
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}
