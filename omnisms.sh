#!/usr/bin/env bash
#
# OmniSMS 管理脚本
# 用法: ./omnisms.sh {start|stop|restart|status|logs|update|run|install-service|uninstall-service}
#
set -euo pipefail

# ==================== 配置 ====================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
REPO_URL="https://github.com/Yanel85/OmniSMS.git"
VENV_DIR="${PROJECT_DIR}/.venv"
PID_FILE="${PROJECT_DIR}/omnisms.pid"
LOG_FILE="${PROJECT_DIR}/omnisms.log"
HOST="0.0.0.0"
PORT="8000"
BRANCH="main"
SERVICE_NAME="omnisms"
SERVICE_TEMPLATE="${PROJECT_DIR}/${SERVICE_NAME}.service"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

# ==================== 工具函数 ====================
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
err() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; }

is_running() {
  [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null
}

# ==================== 虚拟环境 ====================
# prepare_venv [force]
#   不存在 .venv 时创建并安装依赖; force=1 时无论是否存在都重新安装依赖
prepare_venv() {
  local force="${1:-0}"

  if [[ ! -d "${VENV_DIR}" ]]; then
    log "未检测到虚拟环境，正在创建 .venv ..."
    if ! python3 -m venv "${VENV_DIR}" 2>/dev/null; then
      err "创建虚拟环境失败。请先安装 python3-venv："
      err "  Debian/Ubuntu: sudo apt install python3-venv python3-pip"
      err "  CentOS/RHEL:   sudo yum install python3-venv"
      exit 1
    fi
    force=1
  fi

  if [[ "${force}" -eq 1 ]]; then
    log "正在安装 / 更新依赖 (pip install -r requirements.txt) ..."
    "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
    "${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
  fi
}

# ==================== 从 GitHub 拉取源码 (覆盖本地) ====================
pull_source() {
  log "正在从 GitHub 拉取最新源码并覆盖本地文件 ..."
  cd "${PROJECT_DIR}"
  if [[ ! -d "${PROJECT_DIR}/.git" ]]; then
    git init -q
    git remote add origin "${REPO_URL}"
  fi
  git fetch -q origin "${BRANCH}"
  git reset --hard "origin/${BRANCH}"
  git clean -fd -q
  log "源码已更新到最新 (${BRANCH})。"
}

# ==================== 启动 / 停止 ====================
do_start() {
  if is_running; then
    log "OmniSMS 已在运行 (PID $(cat "${PID_FILE}"))."
    return 0
  fi
  prepare_venv 0
  log "正在启动 OmniSMS (http://${HOST}:${PORT}) ..."
  nohup "${VENV_DIR}/bin/python" "${PROJECT_DIR}/web.py" \
    --host "${HOST}" --port "${PORT}" >> "${LOG_FILE}" 2>&1 &
  echo $! > "${PID_FILE}"
  sleep 1
  if is_running; then
    log "启动成功，PID $(cat "${PID_FILE}")。日志: ${LOG_FILE}"
  else
    err "启动失败，请查看日志: ${LOG_FILE}"
    rm -f "${PID_FILE}"
    exit 1
  fi
}

# 前台运行 (供 systemd 使用, 不守护进程)
do_run() {
  prepare_venv 0
  exec "${VENV_DIR}/bin/python" "${PROJECT_DIR}/web.py" \
    --host "${HOST}" --port "${PORT}"
}

do_stop() {
  if ! is_running; then
    log "OmniSMS 未运行。"
    rm -f "${PID_FILE}"
    return 0
  fi
  local pid
  pid="$(cat "${PID_FILE}")"
  log "正在停止 OmniSMS (PID ${pid}) ..."
  kill -TERM "${pid}" 2>/dev/null || true
  for _ in $(seq 1 10); do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "${pid}" 2>/dev/null; then
    log "进程未响应，强制终止 ..."
    kill -9 "${pid}" 2>/dev/null || true
  fi
  rm -f "${PID_FILE}"
  log "已停止。"
}

do_restart() {
  do_stop
  do_start
}

do_status() {
  if is_running; then
    log "OmniSMS 正在运行 (PID $(cat "${PID_FILE}"))."
    return 0
  else
    log "OmniSMS 未运行。"
    return 3
  fi
}

do_logs() {
  if [[ -f "${LOG_FILE}" ]]; then
    tail -n 50 -f "${LOG_FILE}"
  else
    err "日志文件不存在: ${LOG_FILE}"
    exit 1
  fi
}

do_update() {
  log "开始更新 OmniSMS ..."
  do_stop
  pull_source
  prepare_venv 1
  log "更新完成。可执行 '$(basename "$0") start' 启动。"
}

# ==================== systemd 服务 ====================
do_install_service() {
  if [[ "$(id -u)" -ne 0 ]]; then
    err "安装 systemd 服务需要 root 权限，请使用 sudo 运行。"
    exit 1
  fi
  if [[ ! -f "${SERVICE_TEMPLATE}" ]]; then
    err "找不到服务模板文件: ${SERVICE_TEMPLATE}"
    exit 1
  fi

  local owner
  owner="$(stat -c '%U' "${PROJECT_DIR}")"

  log "正在生成并安装 systemd 服务 (${SERVICE_DEST}) ..."
  sed -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
      -e "s|__USER__|${owner}|g" \
      "${SERVICE_TEMPLATE}" > "${SERVICE_DEST}"

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"
  log "服务已安装并设为开机自启。可执行以下命令控制:"
  log "  sudo systemctl start   ${SERVICE_NAME}   # 启动"
  log "  sudo systemctl status  ${SERVICE_NAME}   # 查看状态"
  log "  sudo systemctl enable  ${SERVICE_NAME}   # 确认开机自启"
}

do_uninstall_service() {
  if [[ "$(id -u)" -ne 0 ]]; then
    err "卸载 systemd 服务需要 root 权限，请使用 sudo 运行。"
    exit 1
  fi
  if [[ ! -f "${SERVICE_DEST}" ]]; then
    log "systemd 服务未安装。"
    return 0
  fi
  systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
  rm -f "${SERVICE_DEST}"
  systemctl daemon-reload
  log "systemd 服务已卸载。"
}

# ==================== 入口 ====================
usage() {
  cat <<EOF
用法: $(basename "$0") <命令>

命令:
  start              启动 OmniSMS (守护进程)
  stop               停止 OmniSMS
  restart            重启 OmniSMS
  status             查看运行状态
  logs               实时查看日志 (tail -f)
  update             拉取 GitHub 最新源码并更新依赖后停止 (需手动 start)
  run                前台运行 (供 systemd 调用, 不守护)
  install-service    安装并启用 systemd 开机自启服务 (需 root)
  uninstall-service  卸载 systemd 服务 (需 root)
EOF
}

main() {
  local cmd="${1:-}"
  shift || true
  case "${cmd}" in
    start)             do_start ;;
    stop)              do_stop ;;
    restart)           do_restart ;;
    status)            do_status ;;
    logs)              do_logs ;;
    update)            do_update ;;
    run)               do_run ;;
    install-service)   do_install_service ;;
    uninstall-service) do_uninstall_service ;;
    ""|-h|--help|help) usage ;;
    *) err "未知命令: ${cmd}"; usage; exit 1 ;;
  esac
}

main "$@"
