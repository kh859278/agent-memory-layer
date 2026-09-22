<#
.SYNOPSIS
  agent-memory-layer 一键安装（Windows / PowerShell 5.1+）

.DESCRIPTION
  只做四件事，每一步都打印在做什么：
    1) 检查 Python >= 3.9 与 git
    2) 用 uv（首选）/ pipx / pip --user 装上 aml 命令行
    3) 可选：装上本地记忆服务后端 mcp-memory-service（aml 的存储与检索后端）
    4) aml init 建数据目录 + aml doctor 体检

  幂等：已经装过就跳过（要升级加 -Upgrade）。不改注册表、不动系统目录、不需要管理员。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Ref v0.1.0   # 钉版本（推荐）
  powershell -ExecutionPolicy Bypass -File install.ps1              # 默认 Ref=main（脚本会提醒未钉版本）
  powershell -ExecutionPolicy Bypass -File install.ps1 -DryRun     # 只看会做什么
  powershell -ExecutionPolicy Bypass -File install.ps1 -Upgrade
  powershell -ExecutionPolicy Bypass -File install.ps1 -NoBackend -NoInit
#>
[CmdletBinding()]
param(
    [switch]$Upgrade,
    [switch]$NoBackend,
    [switch]$NoInit,
    [switch]$DryRun,
    [string]$Ref = "main"
)

$ErrorActionPreference = "Continue"
# 让子进程（python/aml）按 UTF-8 输出：中文 Windows 控制台默认 GBK，会把中文和 emoji 打成乱码
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$RepoUrl = "https://github.com/kh859278/agent-memory-layer.git"
$Spec    = "git+$RepoUrl@$Ref"
$Backend = "mcp-memory-service"

function Say  ($m) { Write-Host "  $m" }
function Step ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "  [x]  $m" -ForegroundColor Red; exit 1 }
function Run  ($exe, $argv) {
    if ($DryRun) { Say "(dry-run) $exe $($argv -join ' ')"; return 0 }
    & $exe @argv
    return $LASTEXITCODE
}

Write-Host "agent-memory-layer 安装器" -ForegroundColor White
if ($DryRun) { Warn "dry-run 模式：只打印将要执行的命令" }
# 不钉版本就说出来（2026-09-22 加）：ref 是分支时，装的是"上游此刻的内容"，
# 每次安装/升级都等于执行别人刚推的代码 —— 用户有权知道自己在装什么。
if ($Ref -notmatch '^v?\d+\.\d+') {
    Warn "Ref=$Ref 不是版本 tag：装的是该 ref 的最新内容（未固定版本，上游一改你就跟着变）"
    Say  "  要可复现的安装：install.ps1 -Ref v0.1.0"
}

# ---------------------------------------------------------------- 1. 前置检查
Step "1/4 检查前置条件"

$python = $null
foreach ($cand in @("python", "python3", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $ver = & $cand -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    } catch { continue }
    if ($ver -match '^(\d+)\.(\d+)\.(\d+)') {
        $maj = [int]$Matches[1]; $min = [int]$Matches[2]
        if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 9)) { $python = $cand; Ok "Python $ver（$cand）"; break }
        Warn "$cand 是 Python $ver，需要 >= 3.9"
    }
}
if (-not $python) { Die "找不到 Python >= 3.9，请先装：https://www.python.org/downloads/" }

if (Get-Command git -ErrorAction SilentlyContinue) { Ok "git 已就绪" }
else { Die "找不到 git，请先装：https://git-scm.com/downloads" }

# ------------------------------------------------------- 2. 选包管理器装 CLI
Step "2/4 安装 aml 命令行"

$uv   = Get-Command uv   -ErrorAction SilentlyContinue
$pipx = Get-Command pipx -ErrorAction SilentlyContinue
if ($uv)        { $tool = "uv";   $installArgs = @("tool", "install") }
elseif ($pipx)  { $tool = "pipx"; $installArgs = @("install") }
else            { $tool = "pip";  $installArgs = @("-m", "pip", "install", "--user") }
if ($Upgrade) {
    if ($tool -eq "uv")   { $installArgs += "--force" }
    if ($tool -eq "pipx") { $installArgs += "--force" }
    if ($tool -eq "pip")  { $installArgs += "--upgrade" }
}
Ok "使用 $tool"

$existing = Get-Command aml -ErrorAction SilentlyContinue
if ($existing -and -not $Upgrade) {
    Ok "aml 已经装好了：$($existing.Source)"
    Say "要升级：install.ps1 -Upgrade"
} else {
    $rc = Run $tool ($installArgs + @($Spec))
    if ($rc -ne 0) { Die "$tool 安装失败（看上面的输出）" }
    if (-not $DryRun) { Ok "aml 安装完成" }
}

# 把 uv/pipx 的 bin 目录加进本次会话的 PATH（否则刚装完这一句就找不到 aml）
foreach ($p in @("$env:USERPROFILE\.local\bin", "$env:APPDATA\Python\Scripts",
                 "$env:APPDATA\uv\tools\bin")) {
    if ((Test-Path $p) -and ($env:PATH -notlike "*$p*")) { $env:PATH = "$p;$env:PATH" }
}

# ------------------------------------------------------------ 3. 记忆服务后端
Step "3/4 记忆服务后端（$Backend）"
if ($NoBackend) {
    Warn "按 -NoBackend 跳过"
} elseif (Get-Command memory -ErrorAction SilentlyContinue) {
    Ok "后端已就绪：$((Get-Command memory).Source)"
    Say "启动：memory server --http    （监听 127.0.0.1:8000）"
} else {
    Say "aml 只做采集/蒸馏/检索/治理，向量库里子由 $Backend 提供（本地 HTTP + MCP）"
    $rc = Run $tool ($installArgs + @($Backend))
    if ($rc -ne 0) { Warn "$Backend 安装失败，可稍后手动装；aml 本体已可用" }
    else { Ok "后端安装完成，启动：memory server --http" }
}

# ------------------------------------------------------------- 4. 初始化 + 体检
Step "4/4 初始化与体检"
$a = Get-Command aml -ErrorAction SilentlyContinue
if (-not $a) {
    Warn "本次会话里还找不到 aml：请重开一个终端，再跑 aml init"
    exit 0
}
if ($NoInit) {
    Warn "按 -NoInit 跳过 aml init"
} else {
    $home_ = if ($env:AML_HOME) { $env:AML_HOME } else { Join-Path $env:USERPROFILE "aml" }
    $cfg = if ($env:AML_CONFIG) { $env:AML_CONFIG } else { Join-Path $home_ "config.yaml" }
    if (Test-Path $cfg) { Ok "已有配置，跳过 init：$cfg" }
    else { Run "aml" @("init") | Out-Null; Ok "数据目录与配置已创建（默认 $home_）" }
}
Say "体检（没起后端时会提示连不上，属正常）："
Run "aml" @("doctor") | Out-Null

Write-Host "`n完成。下一步：" -ForegroundColor White
Say "1) 起后端：memory server --http            （或让它开机自启，见 README）"
Say "2) 记基线：aml watch --seed                （首次别把全机历史一次灌进去）"
Say "3) 采集：  aml sync                        （之后交给 aml watch 常驻）"
Say '4) 检索：  aml search --phase P2 "关键词"'
Say "5) 让 agent 结尾播报技能更新：把 README「技能治理」那段规则贴进你的 AGENTS.md"
Say "协议与阶段说明：docs/PROTOCOL.md"
