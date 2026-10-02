# pack-runtime.ps1 —— 构建后自动打包嵌入式 Python 运行时与 hosts 脚本到输出目录。
# 使发布出的程序在目标机器上无需安装 Python 即可运行。
# 用法（由 AccelGui.csproj 的 AfterBuild target 调用）：
#   powershell -File pack-runtime.ps1 -TargetDir <输出目录> -ScriptRoot <hosts 目录>
param(
    [Parameter(Mandatory = $true)][string]$TargetDir,
    [Parameter(Mandatory = $true)][string]$ScriptRoot
)
$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg) { Write-Host "[pack-runtime] $msg" -ForegroundColor Cyan }

# 1) 探测构建机上的 Python（路径动态获取，不写死）。构建机没有 Python 时跳过嵌入式运行时，
#    程序仍可回退到系统 Python（仅构建机环境受影响）。
$pyDir = $null
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) {
    try {
        $pyDir = (& python -c "import sys,os;print(os.path.dirname(sys.executable))" 2>$null).Trim()
    } catch { $pyDir = $null }
}
if (-not $pyDir -or -not (Test-Path (Join-Path $pyDir 'python3*.dll'))) {
    Write-Warning "未找到可用的 Python 安装，跳过嵌入式运行时打包（程序将回退到系统 Python）。"
    $pyDir = $null
}

# 2) 复制嵌入式运行时 -> <TargetDir>\runtime\python\
$pyDst = Join-Path $TargetDir 'runtime\python'
if ($pyDir) {
    $dst = $pyDst
    New-Item -ItemType Directory -Force -Path $dst | Out-Null

    $dll = Get-ChildItem $pyDir -Filter 'python3*.dll' |
        Where-Object { $_.Name -match '^python3\d+\.dll$' } | Select-Object -First 1
    if (-not $dll) {
        Write-Warning "Python 目录中未找到 python3xx.dll，跳过嵌入式运行时打包。"
    } else {
        Copy-Item $dll.FullName (Join-Path $dst $dll.Name) -Force
        # python3.dll（稳定 ABI 桥）可选，带上以防扩展模块需要
        $abi = Join-Path $pyDir 'python3.dll'
        if (Test-Path $abi) { Copy-Item $abi (Join-Path $dst 'python3.dll') -Force }
        Copy-Item (Join-Path $pyDir 'DLLs') (Join-Path $dst 'DLLs') -Recurse -Force

        # Lib 用 robocopy 拷贝并剔除体积大/用不到的目录：
        #  site-packages —— pip 第三方包（本例可占 1GB+），本项目只依赖标准库
        #  test / idlelib / turtledemo / ensurepip / venv / lib2to3 / pydoc_data —— 开发辅助
        #  tcl / tkinter —— tk 图形库（WPF 程序不需要）
        #  __pycache__ / *.pyc —— 字节码缓存，运行时可重新生成
        $libDst = Join-Path $dst 'Lib'
        # 目标可能是上一次构建的脏拷贝，先整体删除再干净复制
        if (Test-Path $libDst) { Remove-Item $libDst -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $libDst | Out-Null
        # 逐顶层目录复制并跳过用不到的（site-packages 是 pip 第三方包，本项目只用标准库）
        $xcld = @('site-packages','test','idlelib','turtledemo','ensurepip','venv','lib2to3','pydoc_data','tcl','tkinter')
        $libSrc = Join-Path $pyDir 'Lib'
        Get-ChildItem $libSrc -File | Copy-Item -Destination $libDst -Force
        Get-ChildItem $libSrc -Directory | Where-Object { $_.Name -notin $xcld } |
            ForEach-Object { Copy-Item $_.FullName (Join-Path $libDst $_.Name) -Recurse -Force }
        # 字节码缓存运行时可重新生成，删除以减小体积
        Get-ChildItem $libDst -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
        Get-ChildItem $libDst -Recurse -File -Filter '*.pyc' | Remove-Item -Force
        Write-Step "嵌入式 Python 运行时已复制到 $(Join-Path $dst '')（DLL=$($dll.Name)）"
    }
}

# 2.5) VC++ 运行库（python3xx.dll 的依赖，如 vcruntime140.dll / vcruntime140_1.dll）
#      Windows Sandbox 等干净系统默认没有 VC++ Redistributable，python DLL 加载失败时
#      pythonnet 会抛 "The type initializer for 'Delegates' threw an exception"。
#      从构建机 System32 探测分发到：应用根目录（标准 DLL 搜索优先）+ python DLL 同目录（双保险）。
$sys32 = Join-Path $env:SystemRoot 'System32'
$vcCopied = @()
foreach ($vcName in @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')) {
    $vcSrc = Join-Path $sys32 $vcName
    if (Test-Path $vcSrc) {
        Copy-Item $vcSrc (Join-Path $TargetDir $vcName) -Force
        if ($pyDst) { Copy-Item $vcSrc (Join-Path $pyDst $vcName) -Force }
        $vcCopied += $vcName
    }
}
if ($vcCopied.Count -gt 0) {
    Write-Step "VC++ 运行库已随程序分发: $($vcCopied -join ', ')"
} else {
    Write-Warning "构建机 System32 未探测到 VC++ 运行库 DLL；若目标机未装 VC++ Redistributable，加载 Python 将失败"
}

# 3) 复制 hosts 脚本 -> <TargetDir>\hosts\（accel_server.py / hosts_accel.py，ips.conf 可选）
$hostsDst = Join-Path $TargetDir 'hosts'
New-Item -ItemType Directory -Force -Path $hostsDst | Out-Null
foreach ($name in @('accel_server.py', 'hosts_accel.py')) {
    $src = Join-Path $ScriptRoot $name
    if (Test-Path $src) { Copy-Item $src (Join-Path $hostsDst $name) -Force }
    else { Write-Warning "缺少脚本文件: $src" }
}
$conf = Join-Path $ScriptRoot 'ips.conf'
if (Test-Path $conf) { Copy-Item $conf (Join-Path $hostsDst 'ips.conf') -Force }
Write-Step "hosts 脚本已复制到 $hostsDst"
