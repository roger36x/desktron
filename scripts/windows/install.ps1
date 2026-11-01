# Desktron Windows 安装器：准备托管 Python → 同步 payload 到规范家目录 → 安装原生壳 →
# 准备工作区与命令行入口 → 隔离验收 → 启动。
#
# 用法(解压发行包后，在包目录中)：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
# 可选参数：
#   -NoLaunch        安装完成后不启动 Desktron
#   -SkipInstallCheck  跳过隔离后端验收(仅排障时使用)
#
# 不需要管理员权限：全部内容装在当前用户目录下。凭证与登录态不在安装包内。
[CmdletBinding()]
param(
    [switch]$NoLaunch,
    [switch]$SkipInstallCheck
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Share = Join-Path $env:USERPROFILE '.local\share\desktron'
$Dest = Join-Path $Share 'home'
$Runtime = Join-Path $Share 'runtime'
$LocalBin = Join-Path $env:USERPROFILE '.local\bin'
$AppDir = Join-Path $env:LOCALAPPDATA 'Programs\Desktron'

function Step($text) { Write-Host "→ $text" }
function Fail($text) { Write-Host "安装未完成：$text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 环境检查
if (-not [Environment]::Is64BitOperatingSystem) { Fail '需要 64 位 Windows 10 或 Windows 11。' }
$build = [Environment]::OSVersion.Version.Build
if ($build -lt 17763) { Fail "需要 Windows 10 1809(内部版本 17763)或更高版本，当前为 $build。" }
if (-not (Test-Path (Join-Path $Here 'payload\desktron\lib\desk_launcher.py'))) { Fail '安装包不完整：缺少 payload。' }
if (-not (Test-Path (Join-Path $Here 'Desktron.exe'))) { Fail '安装包不完整：缺少 Desktron.exe。' }

# Refuse before touching runtime or payload, not after overwriting live code.
$running = Get-Process -Name 'Desktron' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$AppDir*" }
if ($running) { Fail 'Desktron 正在运行。请先在应用内退出，再重新运行安装器。' }

function Test-WebView2 {
    $id = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    $keys = @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$id",
              "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id",
              "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id")
    foreach ($key in $keys) {
        $value = (Get-ItemProperty -Path $key -Name pv -ErrorAction SilentlyContinue).pv
        if ($value -and $value -ne '0.0.0.0') { return $true }
    }
    return $false
}

if (-not (Test-WebView2)) {
    Step '安装 Microsoft Edge WebView2 Runtime(Windows 11 已自带，Windows 10 可能缺少)'
    $boot = Join-Path $env:TEMP 'MicrosoftEdgeWebview2Setup.exe'
    Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $boot -UseBasicParsing
    Start-Process -FilePath $boot -ArgumentList '/silent', '/install' -Wait
    if (-not (Test-WebView2)) { Fail 'WebView2 Runtime 安装后仍不可用，请从 https://go.microsoft.com/fwlink/p/?LinkId=2124703 手动安装后重试。' }
}

# ---------------------------------------------------------------- 托管 Python
. (Join-Path $Here 'payload\scripts\windows\python_runtime.ps1')
Step '准备 Desk 托管 Python'
try {
    $Python = Install-DeskPython -Spec (Join-Path $Here 'payload\desktron\lib\provision\python-runtime.json') -Runtime $Runtime
} catch {
    Fail $_.Exception.Message
}
$PythonHome = Split-Path -Parent $Python

Step '准备 Desk Python 运行依赖'
$requirements = @((Join-Path $Here 'payload\desktron\lib\provision\python-requirements.txt'),
                  (Join-Path $Here 'payload\desktron\lib\provision\python-requirements-windows.txt'))
foreach ($file in $requirements) {
    & $Python -m pip --isolated --disable-pip-version-check install --no-input --requirement $file
    if ($LASTEXITCODE -ne 0) { Fail 'Python 运行依赖准备失败，请检查网络后重试。' }
}

# ---------------------------------------------------------------- 同步 payload(先备份，失败回滚)
Step "同步 payload 到 $Dest"
New-Item -ItemType Directory -Force -Path $Share | Out-Null
$Backup = Join-Path $Share 'home.prev'
if ((Test-Path $Dest) -and (Get-ChildItem $Dest -Force | Select-Object -First 1)) {
    $staging = "$Backup.new"
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    & robocopy.exe $Dest $staging /MIR /SL /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -lt 8) {
        Remove-Item -Recurse -Force $Backup -ErrorAction SilentlyContinue
        Move-Item $staging $Backup
    } else {
        Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
        $Backup = ''
    }
} else { $Backup = '' }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
& robocopy.exe (Join-Path $Here 'payload') $Dest /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    Write-Host '同步失败。' -ForegroundColor Red
    if ($Backup -and (Test-Path $Backup)) {
        Remove-Item -Recurse -Force $Dest -ErrorAction SilentlyContinue
        Move-Item $Backup $Dest
        Write-Host '→ 已回滚，原来那份还能用。'
    }
    exit 1
}

# ---------------------------------------------------------------- 原生壳
Step "安装 Desktron 到 $AppDir"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$staged = Join-Path $AppDir 'Desktron.exe.new'
Copy-Item (Join-Path $Here 'Desktron.exe') $staged -Force
Move-Item $staged (Join-Path $AppDir 'Desktron.exe') -Force
foreach ($resource in 'release-rev', 'release-build') {
    $source = Join-Path $Here $resource
    if (Test-Path $source) { Copy-Item $source (Join-Path $AppDir $resource) -Force }
}

$shell = New-Object -ComObject WScript.Shell
$menu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Desktron.lnk'
$link = $shell.CreateShortcut($menu)
$link.TargetPath = Join-Path $AppDir 'Desktron.exe'
$link.WorkingDirectory = $AppDir
$link.Description = 'Desktron'
$link.Save()

# ~/.local/bin 写入用户 PATH(新开终端生效)：desk-task、shepherd 等命令与引擎都落在这里。
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (($userPath -split ';') -contains $LocalBin)) {
    $next = if ($userPath) { "$LocalBin;$userPath" } else { $LocalBin }
    [Environment]::SetEnvironmentVariable('Path', $next, 'User')
    Step '已把 ~\.local\bin 写入用户 PATH(新开终端生效)'
}
$env:Path = "$LocalBin;$PythonHome;$env:Path"

# ---------------------------------------------------------------- 工作区与验收
Step '准备 Desk 工作区'
& $Python (Join-Path $Dest 'desktron\lib\desk_launcher.py') prepare
if ($LASTEXITCODE -ne 0) { Fail 'Desk 工作区准备失败，请查看上方输出。' }

if (-not $SkipInstallCheck) {
    Step '隔离验收后端启动与 Shepherd 状态(不发送模型请求)'
    & $Python (Join-Path $Dest 'desktron\lib\desk_launcher.py') install-check --report (Join-Path $Share 'install-check.json')
    if ($LASTEXITCODE -ne 0) { Fail "隔离验收未通过，报告：$(Join-Path $Share 'install-check.json')" }
}

$rev = Join-Path $Here 'payload\.release-rev'
if (Test-Path $rev) { Copy-Item $rev (Join-Path $Share '.update-rev') -Force }
$buildFile = Join-Path $Here 'payload\.release-build'
if (Test-Path $buildFile) { Copy-Item $buildFile (Join-Path $Share '.update-build') -Force }

Write-Host ''
if ($SkipInstallCheck) {
    Write-Warning '本次跳过了离线验收，仅可用于排障，不能作为交付验收通过的证据。'
} else {
    Write-Host '离线安装验收通过；引擎登录、模型连接和原生交互尚需首次启动验证。'
}
if ($NoLaunch) {
    Write-Host '完成。可从开始菜单打开 Desktron。'
} else {
    Write-Host '完成。正在打开 Desktron，请按页面引导完成配置。'
    Start-Process -FilePath (Join-Path $AppDir 'Desktron.exe')
}
