# Desk 托管 Python(Windows)：安装器与 CI 共用。版本取自 desktron/lib/provision/python-runtime.json，
# 下载 python-build-standalone 的 install_only 包，按 SHA256SUMS 校验后解包到版本槽。
# 不使用、也不修改系统 Python。

function Test-DeskPython([string]$Exe) {
    if (-not (Test-Path $Exe)) { return $false }
    & $Exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Install-DeskPython {
    param(
        [Parameter(Mandatory = $true)][string]$Spec,
        [Parameter(Mandatory = $true)][string]$Runtime
    )
    $value = Get-Content $Spec -Raw | ConvertFrom-Json
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
    $tripleArch = if ($arch -eq 'arm64') { 'aarch64' } else { 'x86_64' }
    $home_ = Join-Path $Runtime ("python-{0}-windows-{1}" -f $value.version, $arch)
    $python = Join-Path $home_ 'python.exe'
    if (-not (Test-DeskPython $python)) {
        New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
        $name = "cpython-{0}+{1}-{2}-pc-windows-msvc-install_only.tar.gz" -f $value.version, $value.tag, $tripleArch
        $base = "https://github.com/astral-sh/python-build-standalone/releases/download/{0}" -f $value.tag
        $work = Join-Path $Runtime ("python-setup-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        try {
            $archive = Join-Path $work 'python.tar.gz'
            Invoke-WebRequest -Uri ("$base/" + $name.Replace('+', '%2B')) -OutFile $archive -UseBasicParsing
            Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile (Join-Path $work 'SHA256SUMS') -UseBasicParsing
            $expected = @(Get-Content (Join-Path $work 'SHA256SUMS') | Where-Object { ($_ -split '\s+')[1] -eq $name } |
                ForEach-Object { ($_ -split '\s+')[0] })
            $actual = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower()
            if ($expected.Count -ne 1 -or $expected[0] -ne $actual) { throw 'Python 安装包校验失败；未解包或替换运行时。' }
            & tar.exe -xzf $archive -C $work | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Python 安装包解包失败。' }
            if (-not (Test-DeskPython (Join-Path $work 'python\python.exe'))) { throw '下载的 Python 版本或架构不合格。' }
            if (Test-Path $home_) { Move-Item $home_ (Join-Path $work 'previous') | Out-Null }
            Move-Item (Join-Path $work 'python') $home_ | Out-Null
        } finally {
            Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
        }
    }
    # 技能与脚本按 python3 调用；python.exe 只是加载同目录 DLL 的启动器，复制一份即可。
    $python3 = Join-Path $home_ 'python3.exe'
    if (-not (Test-Path $python3)) { Copy-Item $python $python3 | Out-Null }
    return $python
}
