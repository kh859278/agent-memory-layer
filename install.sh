#!/bin/sh
# agent-memory-layer 一键安装（macOS / Linux）
#
#   1) 检查 python3 >= 3.9 与 git
#   2) 用 uv（首选）/ pipx / pip3 装上 aml 命令行
#   3) 可选：装上本地记忆服务后端 mcp-memory-service（aml 的存储与检索后端）
#   4) aml init 建数据目录 + aml doctor 体检
#
# 幂等：装过就跳过（升级加 --upgrade）。不 sudo、不动系统目录。
#
# 用法：
#   sh install.sh                 # 正常安装
#   sh install.sh --dry-run       # 只打印会做什么
#   sh install.sh --upgrade
#   sh install.sh --no-backend --no-init
set -eu
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

REPO_URL="https://github.com/kh859278/agent-memory-layer.git"
REF="${AML_REF:-main}"
SPEC="git+${REPO_URL}@${REF}"
BACKEND="mcp-memory-service"

UPGRADE=0; NO_BACKEND=0; NO_INIT=0; DRY=0
for arg in "$@"; do
  case "$arg" in
    --upgrade)    UPGRADE=1 ;;
    --no-backend) NO_BACKEND=1 ;;
    --no-init)    NO_INIT=1 ;;
    --dry-run)    DRY=1 ;;
    -h|--help)    sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（-h 看用法）"; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '  [ok] %s\n' "$*"; }
warn() { printf '  [!]  %s\n' "$*"; }
die()  { printf '  [x]  %s\n' "$*" >&2; exit 1; }
run() {
  if [ "$DRY" = "1" ]; then say "(dry-run) $*"; return 0; fi
  "$@"
}

echo "agent-memory-layer 安装器"
[ "$DRY" = "1" ] && warn "dry-run 模式：只打印将要执行的命令"

# ------------------------------------------------------------- 1. 前置检查
step "1/4 检查前置条件"
command -v git >/dev/null 2>&1 && ok "git 已就绪" || die "找不到 git，请先安装"

PY=""
for cand in python3 python; do
  command -v "$cand" >/dev/null 2>&1 || continue
  ver=$("$cand" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)
  [ -n "$ver" ] || continue
  major=${ver%%.*}; rest=${ver#*.}; minor=${rest%%.*}
  if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 9 ]; }; then
    PY="$cand"; ok "Python $ver（$cand）"; break
  fi
  warn "$cand 是 Python $ver，需要 >= 3.9"
done
[ -n "$PY" ] || die "找不到 Python >= 3.9"

# --------------------------------------------------------- 2. 装 aml 命令行
step "2/4 安装 aml 命令行"
if command -v uv >/dev/null 2>&1;        then TOOL=uv;   SET=(uv tool install)
elif command -v pipx >/dev/null 2>&1;    then TOOL=pipx; SET=(pipx install)
else                                          TOOL=pip;  SET=("$PY" -m pip install --user)
fi
if [ "$UPGRADE" = "1" ]; then
  case "$TOOL" in
    uv|pipx) SET=("${SET[@]}" --force) ;;
    pip)     SET=("${SET[@]}" --upgrade) ;;
  esac
fi
ok "使用 $TOOL"

if command -v aml >/dev/null 2>&1 && [ "$UPGRADE" != "1" ]; then
  ok "aml 已经装好了：$(command -v aml)"
  say "要升级：sh install.sh --upgrade"
else
  run "${SET[@]}" "$SPEC" || die "$TOOL 安装失败（看上面的输出）"
  [ "$DRY" = "1" ] || ok "aml 安装完成"
fi
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac

# --------------------------------------------------------- 3. 记忆服务后端
step "3/4 记忆服务后端（$BACKEND）"
if [ "$NO_BACKEND" = "1" ]; then
  warn "按 --no-backend 跳过"
elif command -v memory >/dev/null 2>&1; then
  ok "后端已就绪：$(command -v memory)"
  say "启动：memory server --http    （监听 127.0.0.1:8000）"
else
  say "aml 只做采集/蒸馏/检索/治理，向量库里子由 $BACKEND 提供（本地 HTTP + MCP）"
  if run "${SET[@]}" "$BACKEND"; then ok "后端安装完成，启动：memory server --http"
  else warn "$BACKEND 安装失败，可稍后手动装；aml 本体已可用"; fi
fi

# --------------------------------------------------------- 4. 初始化 + 体检
step "4/4 初始化与体检"
if ! command -v aml >/dev/null 2>&1; then
  warn "当前 shell 里还找不到 aml：重开终端后跑 aml init"
  exit 0
fi
if [ "$NO_INIT" = "1" ]; then
  warn "按 --no-init 跳过 aml init"
else
  HOME_DIR="${AML_HOME:-$HOME/aml}"
  CFG="${AML_CONFIG:-$HOME_DIR/config.yaml}"
  if [ -f "$CFG" ]; then ok "已有配置，跳过 init：$CFG"
  else run aml init >/dev/null && ok "数据目录与配置已创建（默认 $HOME_DIR）"; fi
fi
say "体检（没起后端时会提示连不上，属正常）："
run aml doctor || true

printf '\n完成。下一步：\n'
say "1) 起后端：memory server --http            （或让它开机自启，见 README）"
say "2) 记基线：aml watch --seed                （首次别把全机历史一次灌进去）"
say "3) 采集：  aml sync                        （之后交给 aml watch 常驻）"
say '4) 检索：  aml search --phase P2 "关键词"'
say "5) 让 agent 结尾播报技能更新：把 README「技能治理」那段规则贴进你的 AGENTS.md"
say "协议与阶段说明：docs/PROTOCOL.md"
