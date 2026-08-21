#!/bin/bash
# 生成自签 TLS 证书，供 Web 服务以 https 提供（手机浏览器需 https 才能网页内录音）。
# 证书 SAN 覆盖 localhost + 127.0.0.1 + 本机全部局域网 IPv4。
# 局域网 IP 变化后重跑本脚本即可，然后重启 ollama-voice-server 服务。
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
CERT_DIR="$DIR/certs"
mkdir -p "$CERT_DIR"

# 收集本机局域网 IPv4（排除回环）
SAN="DNS:localhost,IP:127.0.0.1"
for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in
        127.*|*:*) ;;                      # 跳过回环与 IPv6
        *) SAN="$SAN,IP:$ip" ;;
    esac
done

echo "生成自签证书，SAN = $SAN"
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/cert.pem" \
    -days 3650 \
    -subj "/CN=ollama-voice-input" \
    -addext "subjectAltName=$SAN" \
    >/dev/null 2>&1

chmod 600 "$CERT_DIR/key.pem"
chmod 644 "$CERT_DIR/cert.pem"
echo "证书已生成: $CERT_DIR/cert.pem  (有效期 10 年)"
