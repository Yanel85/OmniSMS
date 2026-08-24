#!/bin/bash
# OmniSMS Docker 自动化构建脚本
# 
# 特点:
#   - 自动检测当前系统架构 (x86/ARM)
#   - Docker 环境: 自动生成证书, 启用 HTTPS
#   - 本地环境: 使用 HTTP
#
# 用法:
#   ./build.sh              # 自动检测并构建当前平台镜像
#   ./build.sh run          # 构建并运行容器 (Docker自动HTTPS)

set -e

# ==================== 配置区 ====================
IMAGE_NAME="omnisms"
IMAGE_TAG="latest"
CONTAINER_NAME="omnisms"
WEB_PORT=8000

# ==================== 颜色输出 ====================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
header(){ echo -e "${BLUE}==========================================${NC}"; echo -e "${CYAN}  $1${NC}"; echo -e "${BLUE}==========================================${NC}"; }

# ==================== 平台检测 ====================
detect_arch() {
    local arch=$(uname -m)
    case "$arch" in
        x86_64|amd64)    echo "amd64" ;;
        aarch64|arm64)   echo "arm64" ;;
        armv7l|armhf)    echo "arm32v7" ;;
        *)               error "不支持的系统架构: ${arch}" ;;
    esac
}

# ==================== 端口检测 ====================
is_port_in_use() {
    local port=$1
    if command -v ss &> /dev/null; then
        ss -tuln | grep -q ":${port} " && return 0
    elif command -v netstat &> /dev/null; then
        netstat -tuln | grep -q ":${port} " && return 0
    elif command -v lsof &> /dev/null; then
        lsof -i :"${port}" &> /dev/null && return 0
    fi
    return 1
}

# ==================== 核心构建函数 ====================
do_build() {
    local platform=$(detect_arch)
    local default_tag="${IMAGE_NAME}:${IMAGE_TAG}"
    
    header "开始构建 Docker 镜像"
    info "目标平台: ${platform}"
    
    [ ! -f "Dockerfile" ] && error "Dockerfile 不存在"
    
    info "正在构建镜像 (平台: ${platform})..."
    
    docker build -t "${default_tag}" .
    
    info "✓ 镜像构建成功: ${default_tag}"
    
    echo ""
    docker images "${IMAGE_NAME}" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"
}

# ==================== 运行容器 ====================
do_run() {
    local default_tag="${IMAGE_NAME}:${IMAGE_TAG}"
    
    header "启动容器"
    
    # 确保镜像存在
    if ! docker image inspect "${default_tag}" &> /dev/null; then
        warn "镜像不存在，开始构建..."
        do_build
    fi
    
    # 清理旧容器
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        warn "停止并移除旧容器: ${CONTAINER_NAME}"
        docker stop "${CONTAINER_NAME}" &> /dev/null || true
        docker rm "${CONTAINER_NAME}" &> /dev/null || true
    fi
    
    # 检测端口占用
    while is_port_in_use "${WEB_PORT}"; do
        warn "端口 ${WEB_PORT} 已被占用"
        read -p "请输入新的端口号: " new_port
        if [[ "$new_port" =~ ^[0-9]+$ ]] && [ "$new_port" -ge 1 ] && [ "$new_port" -le 65535 ]; then
            WEB_PORT="$new_port"
        else
            warn "无效端口号，请重新输入 (1-65535)"
        fi
    done
    
    # 启动新容器
    info "启动容器: ${CONTAINER_NAME}"
    info "Docker 模式将自动启用 HTTPS"
    
    docker run -d \
        --name "${CONTAINER_NAME}" \
        --privileged \
        --restart unless-stopped \
        -p "${WEB_PORT}:8000" \
        -v omnisms-logs:/app/logs \
        -v omnisms-db:/app/data \
        -e TZ=Asia/Shanghai \
        "${default_tag}"
    
    if [ $? -eq 0 ]; then
        sleep 3
        echo ""
        info "============================================"
        info "  ✓ 容器启动成功!"
        info "============================================"
        info "  访问地址: https://localhost:${WEB_PORT}"
        info "  协议: HTTPS (Docker自动生成证书)"
        info "  连接模式: WebSerial 桥接 (pySerial 已禁用)"
        echo ""
        warn "  ⚠️  浏览器安全提示 (首次访问):"
        warn "     Chrome: 点击'高级' → '继续前往'"
        warn "     Firefox: 点击'高级' → '接受风险并继续'"
        info "--------------------------------------------"
        info "  查看日志: docker logs -f ${CONTAINER_NAME}"
        info "  进入容器: docker exec -it ${CONTAINER_NAME} bash"
        info "  停止容器: docker stop ${CONTAINER_NAME}"
        info "============================================"
        echo ""
        docker logs --tail 20 "${CONTAINER_NAME}"
    else
        error "容器启动失败"
    fi
}

# ==================== 推送镜像 ====================
do_push() {
    local platform=$(detect_arch)
    local registry="${1:-your-registry}/${IMAGE_NAME}"
    
    header "推送镜像"
    warn "请先修改脚本中的 registry 地址"
    read -p "确认推送? (y/N): " confirm
    
    [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { info "已取消"; return; }
    
    docker push "${registry}:${IMAGE_TAG}-${platform}"
    
    info "✓ 推送完成"
}

# ==================== 清理资源 ====================
do_cleanup() {
    header "清理资源"
    
    docker ps -a --filter "name=${CONTAINER_NAME}" -q | xargs -r docker rm -f &> /dev/null && info "已删除容器" || true
    
    read -p "是否删除相关镜像? (y/N): " c
    if [ "$c" = "y" ] || [ "$c" = "Y" ]; then
        docker rmi "${IMAGE_NAME}:${IMAGE_TAG}-amd64" &> /dev/null || true
        docker rmi "${IMAGE_NAME}:${IMAGE_TAG}-arm64" &> /dev/null || true
        docker rmi "${IMAGE_NAME}:${IMAGE_TAG}" &> /dev/null || true
        info "已删除镜像"
    fi
    
    docker image prune -f &> /dev/null || true
    info "✓ 清理完成"
}

# ==================== 帮助信息 ====================
show_help() {
    local arch=$(detect_arch)
    cat << EOF

OmniSMS Docker 构建脚本
  当前架构: ${arch}

用法: $0 [命令]

命令:
  (无)     构建 Docker 镜像 (默认, 当前平台)
  run      构建并运行容器 (自动 HTTPS)
  push     推送镜像到仓库
  cleanup  清理容器和镜像
  logs     查看容器日志
  status   查看运行状态
  shell    进入容器终端
  arch     显示当前架构
  help     显示帮助信息

特性:
  Docker 环境: 自动生成自签名证书, 启用 HTTPS
  本地环境:   直接使用 HTTP (python web.py)

示例:
  $0                  # 自动检测并构建当前平台镜像
  $0 run              # 构建并运行 (Docker自动HTTPS)

EOF
}

# ==================== 主入口 ====================
main() {
    local arg1="$1"
    local command=""
    
    case "$arg1" in
        build|run|push|cleanup|logs|status|shell|arch|help|--help|-h|"")
            command="${arg1:-build}"
            ;;
        *)
            error "未知参数: $1\n运行 '$0 help' 查看帮助"
            ;;
    esac
    
    case "$command" in
        build)  do_build ;;
        run)    do_run ;;
        push)   do_push "$2" ;;
        cleanup) do_cleanup ;;
        logs)   docker logs -f "${CONTAINER_NAME}" 2>/dev/null || error "容器未运行" ;;
        status) 
            echo ""
            info "===== 系统架构 ====="
            echo "  $(uname -m) -> $(detect_arch)"
            echo ""
            info "===== 容器状态 ====="
            docker ps -a --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || warn "无容器"
            echo ""
            info "===== 镜像列表 ====="
            docker images "${IMAGE_NAME}" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" 2>/dev/null || warn "无镜像"
            echo ""
            ;;
        shell)  docker exec -it "${CONTAINER_NAME}" /bin/bash 2>/dev/null || docker exec -it "${CONTAINER_NAME}" /bin/sh || error "无法进入容器" ;;
        arch)   echo "系统架构: $(uname -m) -> $(detect_arch)" ;;
        help)   show_help ;;
    esac
}

main "$@"
