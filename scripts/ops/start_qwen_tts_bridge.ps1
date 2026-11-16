param(
    [switch]$Background,
    [switch]$Offline,
    [ValidateSet("auto", "cpu", "cuda:0")]
    [string]$Device = "cpu"
)

$ErrorActionPreference = "Stop"

$runtimeRoot = "C:\AuroraQwenTTS"
$python = Join-Path $runtimeRoot "venv\Scripts\python.exe"
$cacheRoot = Join-Path $runtimeRoot "hf-cache"
$tempRoot = Join-Path $runtimeRoot "tmp"
$bridge = Join-Path $PSScriptRoot "qwen_tts_bridge.py"

if (-not (Test-Path $python)) {
    throw "Qwen-TTS runtime not found at $python. Install it with scripts/ops/setup_qwen_tts.ps1."
}
if (-not (Test-Path $bridge)) {
    throw "Qwen-TTS bridge source not found at $bridge."
}

New-Item -ItemType Directory -Force -Path $cacheRoot, $tempRoot | Out-Null
$env:HF_HOME = $cacheRoot
$env:TORCH_HOME = Join-Path $runtimeRoot "torch-cache"
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:QWEN_TTS_HOST = "127.0.0.1"
$env:QWEN_TTS_PORT = "8880"
$env:QWEN_TTS_DEVICE = $Device
$env:QWEN_TTS_SUBTALKER_DOSAMPLE = "false"
$env:QWEN_TTS_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
if ($Offline) {
    $env:HF_HUB_OFFLINE = "1"
    $env:TRANSFORMERS_OFFLINE = "1"
}

if ($Background) {
    $stdout = Join-Path $runtimeRoot "qwen-tts-bridge.out.log"
    $stderr = Join-Path $runtimeRoot "qwen-tts-bridge.err.log"
    $process = Start-Process -FilePath $python `
        -ArgumentList ('"{0}"' -f $bridge) `
        -WorkingDirectory $runtimeRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    Write-Output "Started Qwen-TTS bridge process $($process.Id); logs are in $runtimeRoot."
    exit 0
}

& $python $bridge
