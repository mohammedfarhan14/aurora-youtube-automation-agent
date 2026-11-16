param(
    [switch]$Background,
    [string]$RuntimeRoot = "C:\AuroraKokoro",
    [ValidateRange(1024, 65535)]
    [int]$Port = 8881
)

$ErrorActionPreference = "Stop"
$python = Join-Path $RuntimeRoot ".venv\Scripts\python.exe"
$model = Join-Path $RuntimeRoot "api\src\models\v1_0\kokoro-v1_0.pth"
$voice = Join-Path $RuntimeRoot "api\src\voices\v1_0\af_heart.pt"
$config = Join-Path $RuntimeRoot "api\src\models\v1_0\config.json"
$logRoot = Join-Path $RuntimeRoot "aurora-logs"

foreach ($required in @($python, $model, $voice, $config)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Kokoro file not found: $required. This launcher never installs packages or downloads model/voice files."
    }
}

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$env:PHONEMIZER_ESPEAK_LIBRARY = "C:\Program Files\eSpeak NG\libespeak-ng.dll"
$env:PYTHONUTF8 = "1"
$env:PROJECT_ROOT = $RuntimeRoot
$env:USE_GPU = "false"
$env:PYTHONPATH = "$RuntimeRoot;$RuntimeRoot\api"
$env:MODEL_DIR = "src/models"
$env:VOICES_DIR = "src/voices/v1_0"
$env:WEB_PLAYER_PATH = "$RuntimeRoot\web"
$env:DEFAULT_VOICE = "af_heart"
$env:HOST = "127.0.0.1"
$env:PORT = "$Port"

$arguments = @("-m", "uvicorn", "api.src.main:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Background) {
    $stdout = Join-Path $logRoot "kokoro.stdout.log"
    $stderr = Join-Path $logRoot "kokoro.stderr.log"
    $process = Start-Process -FilePath $python -ArgumentList $arguments `
        -WorkingDirectory $RuntimeRoot -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -PassThru
    Write-Output "Started Kokoro TTS (PID $($process.Id)) on port $Port. Logs: $logRoot"
    exit 0
}

& $python @arguments
exit $LASTEXITCODE
