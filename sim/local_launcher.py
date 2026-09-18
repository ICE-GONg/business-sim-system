"""Build the admin-only macOS local compute launcher."""
from __future__ import annotations

import io
from pathlib import Path
import shlex
import zipfile


_WORKER_FILES = (
    "local_worker_server.py",
    "scf_worker.py",
    "sim/__init__.py",
    "sim/bot_joint_pricing.py",
    "sim/bot_market_forecast.py",
    "sim/bots.py",
    "sim/cpi.py",
    "sim/db.py",
    "sim/defaults.py",
    "sim/engine.py",
    "sim/remote_worker.py",
)


def build_local_worker_launcher_zip(token: str) -> bytes:
    """Return a visible, self-diagnosing macOS launcher archive."""
    script = r'''#!/bin/zsh
set -eu
setopt PIPE_FAIL

export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
WORKER_DIR="$HOME/.business-sim-local-worker"
RUNTIME_DIR="$HOME/.business-sim-local-worker-runtime"
SCRIPT_DIR="${0:A:h}"
PAYLOAD_DIR="$SCRIPT_DIR/worker_payload"
LOG_FILE="$RUNTIME_DIR/launcher.log"
mkdir -p "$RUNTIME_DIR"
: >"$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

show_failure() {
  local STATUS="${1:-1}"
  trap - ZERR INT TERM
  echo
  echo "启动失败。完整日志：$LOG_FILE"
  if [[ "${LOCAL_WORKER_NO_DIALOG:-0}" != "1" ]]; then
    /usr/bin/open -a TextEdit "$LOG_FILE" >/dev/null 2>&1 || true
    /usr/bin/osascript - "$LOG_FILE" <<'APPLESCRIPT' || true
on run argv
  display dialog ("本地算力启动失败。\n\n详细日志已经打开：\n" & item 1 of argv) buttons {"好"} default button 1 with icon stop
end run
APPLESCRIPT
    echo "按回车关闭此窗口。"
    read -r || true
  fi
  exit "$STATUS"
}
trap 'show_failure $?' ZERR
trap 'show_failure 130' INT TERM

echo "========================================"
echo "  商赛系统 · 本地算力启动器"
echo "========================================"
echo "[1/5] 检查运行环境"

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "没有找到 Python 3。请先安装 Python 3 后重新运行。"
  false
fi
if [[ ! -f "$PAYLOAD_DIR/local_worker_server.py" || ! -f "$PAYLOAD_DIR/scf_worker.py" ]]; then
  echo "启动器文件不完整。请完整解压 ZIP 后再双击，不能只拖出 .command 文件。"
  false
fi

echo "[2/5] 安装随包附带的计算代码（无需连接 GitHub）"
STAGED_WORKER="$RUNTIME_DIR/worker-code.$$"
/bin/rm -rf "$STAGED_WORKER"
/bin/mkdir -p "$STAGED_WORKER"
/usr/bin/ditto "$PAYLOAD_DIR" "$STAGED_WORKER"
if [[ -e "$WORKER_DIR" ]]; then
  BACKUP_DIR="$RUNTIME_DIR/worker-code-previous"
  /bin/rm -rf "$BACKUP_DIR"
  /bin/mv "$WORKER_DIR" "$BACKUP_DIR"
fi
/bin/mv "$STAGED_WORKER" "$WORKER_DIR"

echo "[3/5] 检查公网连接工具"
CLOUDFLARED_BIN="$(command -v cloudflared || true)"
if [[ -z "$CLOUDFLARED_BIN" ]]; then
  BREW_BIN="$(command -v brew || true)"
  if [[ -z "$BREW_BIN" ]]; then
    echo "没有找到 cloudflared 或 Homebrew。"
    echo "请先从 https://brew.sh 安装 Homebrew，再重新双击启动器。"
    false
  fi
  echo "首次运行需要安装 cloudflared，请稍候……"
  "$BREW_BIN" install cloudflared
  CLOUDFLARED_BIN="$(command -v cloudflared || true)"
fi
if [[ -z "$CLOUDFLARED_BIN" ]]; then
  echo "cloudflared 安装完成后仍无法找到。"
  false
fi

echo "[4/5] 启动本地计算服务"
for NAME in worker tunnel; do
  PID_FILE="$RUNTIME_DIR/$NAME.pid"
  if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && /bin/kill -0 "$OLD_PID" 2>/dev/null; then
      /bin/kill "$OLD_PID" 2>/dev/null || true
    fi
  fi
done
/bin/sleep 0.5

export SUPER_BOT_REMOTE_TOKEN=__SUPER_BOT_REMOTE_TOKEN__
export SUPER_BOT_LOCAL_PORT=8765
nohup "$PYTHON_BIN" "$WORKER_DIR/local_worker_server.py" >"$RUNTIME_DIR/worker.log" 2>&1 &
echo $! >"$RUNTIME_DIR/worker.pid"

WORKER_READY=0
for ATTEMPT in {1..30}; do
  if /usr/bin/curl -fsS --max-time 1 http://127.0.0.1:8765 >/dev/null 2>&1; then
    WORKER_READY=1
    break
  fi
  /bin/sleep 0.5
done
if [[ "$WORKER_READY" != "1" ]]; then
  echo "本地计算服务没有响应。"
  tail -80 "$RUNTIME_DIR/worker.log" 2>/dev/null || true
  false
fi

echo "[5/5] 建立 HTTPS 公网线路"
PUBLIC_URL=""
PUBLIC_READY=0
for TUNNEL_ATTEMPT in 1 2 3; do
  echo "  正在创建线路（第 $TUNNEL_ATTEMPT/3 次）……"
  if [[ -f "$RUNTIME_DIR/tunnel.pid" ]]; then
    OLD_TUNNEL_PID="$(cat "$RUNTIME_DIR/tunnel.pid" 2>/dev/null || true)"
    if [[ -n "$OLD_TUNNEL_PID" ]] && /bin/kill -0 "$OLD_TUNNEL_PID" 2>/dev/null; then
      /bin/kill "$OLD_TUNNEL_PID" 2>/dev/null || true
      /bin/sleep 0.5
    fi
  fi
  if [[ -s "$RUNTIME_DIR/tunnel.log" ]]; then
    /bin/cp "$RUNTIME_DIR/tunnel.log" "$RUNTIME_DIR/tunnel.previous.log"
  fi
  : >"$RUNTIME_DIR/tunnel.log"
  nohup "$CLOUDFLARED_BIN" tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8765 >"$RUNTIME_DIR/tunnel.log" 2>&1 &
  TUNNEL_PID=$!
  echo "$TUNNEL_PID" >"$RUNTIME_DIR/tunnel.pid"

  PUBLIC_URL=""
  for URL_ATTEMPT in {1..90}; do
    PUBLIC_URL="$(grep -aEo 'https://[-a-z0-9]+\.trycloudflare\.com' "$RUNTIME_DIR/tunnel.log" | grep -av '^https://api\.' | tail -1 || true)"
    if [[ -n "$PUBLIC_URL" ]]; then break; fi
    if ! /bin/kill -0 "$TUNNEL_PID" 2>/dev/null; then break; fi
    /bin/sleep 0.5
  done
  if [[ -z "$PUBLIC_URL" ]]; then
    echo "  本次没有获得公网地址，自动换线。"
    tail -8 "$RUNTIME_DIR/tunnel.log" 2>/dev/null || true
    continue
  fi

  echo "  已获得地址，正在验证公网可用性：$PUBLIC_URL"
  PUBLIC_HOST="${PUBLIC_URL#https://}"
  for HEALTH_ATTEMPT in {1..45}; do
    if ! /bin/kill -0 "$TUNNEL_PID" 2>/dev/null; then break; fi
    HEALTH_RESPONSE="$(/usr/bin/curl -fs --max-time 3 "$PUBLIC_URL/health" 2>/dev/null || true)"
    if [[ -z "$HEALTH_RESPONSE" ]] && command -v dig >/dev/null 2>&1; then
      # Some mainland DNS resolvers cache a negative answer for a brand-new
      # Quick Tunnel. AliDNS normally sees it immediately, so use that IP only
      # for this local verification while keeping the normal HTTPS hostname.
      PUBLIC_IP="$(dig @223.5.5.5 +time=2 +tries=1 +short "$PUBLIC_HOST" 2>/dev/null | grep -aE '^[0-9]+(\.[0-9]+){3}$' | head -1 || true)"
      if [[ -n "$PUBLIC_IP" ]]; then
        HEALTH_RESPONSE="$(/usr/bin/curl -fs --max-time 5 --resolve "$PUBLIC_HOST:443:$PUBLIC_IP" "$PUBLIC_URL/health" 2>/dev/null || true)"
      fi
    fi
    if print -r -- "$HEALTH_RESPONSE" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
      PUBLIC_READY=1
      break
    fi
    /bin/sleep 1
  done
  if [[ "$PUBLIC_READY" == "1" ]]; then
    break
  fi
  echo "  本次地址无法从公网访问，自动换线。"
done
if [[ "$PUBLIC_READY" != "1" ]]; then
  echo "三次公网线路均未通过验证。请检查网络后重新双击启动器。"
  tail -100 "$RUNTIME_DIR/tunnel.log" 2>/dev/null || true
  false
fi

print -r -- "$PUBLIC_URL" >"$RUNTIME_DIR/current-url.txt"
print -rn -- "$PUBLIC_URL" | /usr/bin/pbcopy

echo
echo "========================================"
echo "本地算力启动成功"
echo "$PUBLIC_URL"
echo "地址已经复制到剪贴板。"
echo "回到管理员的‘回合控制’，粘贴后点击‘连接本地算力’。"
echo "========================================"

if [[ "${LOCAL_WORKER_NO_DIALOG:-0}" != "1" ]]; then
  /usr/bin/osascript - "$PUBLIC_URL" <<'APPLESCRIPT'
on run argv
  display dialog ("本地算力已启动，HTTPS 地址已经复制：\n\n" & item 1 of argv & "\n\n回到管理员的‘回合控制’页面，粘贴后点击‘连接本地算力’。") buttons {"好"} default button 1
end run
APPLESCRIPT

  echo "按回车关闭此窗口；后台算力会继续运行。"
  read -r || true
fi
trap - ZERR INT TERM
'''.replace("__SUPER_BOT_REMOTE_TOKEN__", shlex.quote(str(token)))

    instructions = """商赛系统本地算力（Mac）\n\n1. 完整解压 ZIP，保留启动文件和 worker_payload 文件夹。\n2. 双击“启动本地算力.command”。\n3. 等待窗口显示五步进度。成功后 HTTPS 地址会自动复制。\n4. 回到管理员 → 回合控制，粘贴地址并点击“连接本地算力”。\n\n计算代码已包含在下载包中，不需要连接 GitHub。\n如果 macOS 阻止打开，请右键启动文件并选择“打开”。\n发生错误时启动器不会闪退，会自动打开日志并停留在错误画面。\n"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        launcher = zipfile.ZipInfo("启动本地算力.command")
        launcher.create_system = 3
        launcher.external_attr = 0o755 << 16
        launcher.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(launcher, script.encode("utf-8"))

        readme = zipfile.ZipInfo("启动说明.txt")
        readme.create_system = 3
        readme.external_attr = 0o644 << 16
        readme.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(readme, instructions.encode("utf-8"))

        project_root = Path(__file__).resolve().parent.parent
        for relative_name in _WORKER_FILES:
            source = project_root / relative_name
            info = zipfile.ZipInfo(f"worker_payload/{relative_name}")
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes())
    return buffer.getvalue()
