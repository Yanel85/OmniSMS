# OmniSMS Docker 镜像
# 支持多架构: amd64 (x86_64) / arm64 (aarch64)
# 
# 特性:
#   - 自动生成自签名证书, 启用 HTTPS
#   - 本地开发: 直接运行 python web.py (HTTP)

FROM python:3.11-slim

LABEL maintainer="OmniSMS"
LABEL description="Air780系列设备短信通话融合管理系统"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# OMNISMS_PASSWORD: 可选访问口令 (默认空 = 免鉴权)。
# 设置后启用共享口令会话 (HttpOnly Cookie, 同源 WebSocket 握手自动携带), 用于保护
# 通过 `docker run -p` 暴露到局域网的端口; 未设置时适用于仅本机访问的场景。
# 通过 docker run -e OMNISMS_PASSWORD=xxx 注入 (build.sh run 会交互式询问)。
ENV OMNISMS_PASSWORD=""

RUN apt-get update && apt-get install -y --no-install-recommends \
    usbutils \
    openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs /app/data /app/certs

EXPOSE 8000

# 健康检查使用 /api/env: 该接口始终免鉴权, 即使设置了 OMNISMS_PASSWORD 也不会
# 因 401 被误判为 unhealthy (此前用 /api/devices, 启用口令后会一直 unhealthy)。
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request, ssl; ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE; urllib.request.urlopen('https://localhost:8000/api/env', context=ctx)" || exit 1

# 启动时自动生成证书并启用 HTTPS
CMD ["/bin/bash", "-c", "if [ ! -f /app/certs/server.crt ]; then echo '生成 SSL 证书...' && openssl genrsa -out /app/certs/server.key 2048 2>/dev/null && openssl req -new -x509 -key /app/certs/server.key -out /app/certs/server.crt -days 3650 -subj '/CN=omnisms.local' -addext 'subjectAltName=DNS:localhost,DNS:*.local,IP:127.0.0.1' 2>/dev/null && echo '✓ 证书已生成'; fi && echo '=== 启动 HTTPS 服务 ===' && python web.py --host 0.0.0.0 --port 8000 --ssl-cert /app/certs/server.crt --ssl-key /app/certs/server.key"]
