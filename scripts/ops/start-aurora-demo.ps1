param(
    [ValidateSet("start", "stop")]
    [string]$Action = "start",
    [string]$Distro = "Auto",
    [string]$LinuxUser = "",
    [string]$ProjectPath = "",
    [switch]$SkipQwenTts,
    [switch]$SkipKokoroTts,
    [switch]$SkipOllama,
    [switch]$SkipComfyUI,
    [string]$ComfyUIPath = "C:\ComfyUI_windows_portable"
)

$ErrorActionPreference = "Stop"

function Assert-AvailablePhysicalMemory {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [Parameter(Mandatory = $true)][double]$MinimumGb
    )
    $freeGb = [double](Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
    if ($freeGb -lt $MinimumGb) {
        throw ("Not starting {0}: Windows has {1:N2} GB physical memory available; " -f $Service, $freeGb) +
            ("this service needs at least {0:N1} GB free for a reliable startup. " -f $MinimumGb) +
            "Close unused GPU/LLM workloads or stop unneeded WSL services, then retry. Existing services were left running."
    }
}

function Stop-AuroraOwnedListener {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$RuntimePath,
        [Parameter(Mandatory = $true)][string]$CommandMarker
    )
    $listenerIds = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($listenerId in $listenerIds) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerId" -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $ownedPath = $process.ExecutablePath -and $process.ExecutablePath.StartsWith($RuntimePath, [StringComparison]::OrdinalIgnoreCase)
        $ownedCommand = $process.CommandLine -and $process.CommandLine.IndexOf($CommandMarker, [StringComparison]::OrdinalIgnoreCase) -ge 0
        $verifiedAuroraService = $ownedPath -and $ownedCommand
        if (-not $verifiedAuroraService -and (Get-Process -Id $listenerId -ErrorAction SilentlyContinue).ProcessName -eq "python") {
            try {
                if ($Port -eq 8188) {
                    if (-not $script:ComfyHealthUrl) { throw "ComfyUI probe address is unavailable." }
                    $null = Invoke-RestMethod -Uri $script:ComfyHealthUrl -TimeoutSec 2
                    $verifiedAuroraService = $true
                } elseif ($Port -eq 8880 -or $Port -eq 8881) {
                    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
                    $verifiedAuroraService = $health.status -eq "healthy"
                }
            } catch { }
        }
        if ($verifiedAuroraService) {
            try {
                Stop-Process -Id $listenerId -ErrorAction Stop
                Write-Host "Stopped Aurora-owned service on port $Port (PID $listenerId)."
            } catch {
                Write-Warning "Could not stop the verified Aurora listener on port $Port (PID $listenerId): $($_.Exception.Message)"
            }
        } else {
            Write-Warning "Left port $Port listener PID $listenerId untouched because its Aurora runtime identity could not be verified."
        }
    }
}

$wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
if (-not $wsl) { throw "wsl.exe was not found. Run this from Windows PowerShell on the configured WSL machine." }

if ($Distro -eq "Auto") {
    $distroFromPath = [regex]::Match(
        $PSScriptRoot,
        '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if (-not $distroFromPath.Success) {
        throw "Could not determine the WSL distribution from '$PSScriptRoot'. Re-run this script from its \\wsl.localhost\<distro> path or pass -Distro explicitly."
    }
    $Distro = $distroFromPath.Groups[1].Value
}
if (-not $LinuxUser) {
    $LinuxUser = (& $wsl.Source -d $Distro -- bash -lc "id -un").Trim()
    if (-not $LinuxUser) { throw "Could not determine the default Linux user in WSL '$Distro'. Pass -LinuxUser explicitly." }
}
if (-not $ProjectPath) {
    $wslScriptPath = [regex]::Match(
        $PSScriptRoot,
        '^\\\\wsl(?:\.localhost|\$)\\[^\\]+(?<linuxPath>\\.*)$',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if (-not $wslScriptPath.Success) {
        throw "Could not infer the Linux project path from '$PSScriptRoot'. Pass -ProjectPath explicitly."
    }
    $ProjectPath = $wslScriptPath.Groups['linuxPath'].Value -replace '\\', '/'
    $ProjectPath = $ProjectPath -replace '/scripts/ops$', ''
}
Write-Host "Using WSL distribution '$Distro' and user '$LinuxUser' for $ProjectPath."

$pathCheck = "test -d '$ProjectPath'"
$pathFound = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    & $wsl.Source -d $Distro -u $LinuxUser -- bash -lc $pathCheck *> $null
    if ($LASTEXITCODE -eq 0) {
        $pathFound = $true
        break
    }
    if ($attempt -lt 3) { Start-Sleep -Seconds 1 }
}
if (-not $pathFound) {
    throw "Project path '$ProjectPath' was not found in WSL distribution '$Distro'. Pass the distribution that contains the repository with -Distro."
}

if ($Action -eq "stop") {
    $comfyWslHostIp = (& $wsl.Source -d $Distro -u $LinuxUser -- bash -lc "ip -4 route show default | cut -d' ' -f3 | head -n1").Trim()
    if ($comfyWslHostIp -match '^([0-9]{1,3}\.){3}[0-9]{1,3}$') {
        $script:ComfyHealthUrl = "http://${comfyWslHostIp}:8188/system_stats"
    }
    & $wsl.Source -d $Distro -u $LinuxUser -- bash -lc "cd '$ProjectPath' && AURORA_WINDOWS_ORCHESTRATED=1 ./scripts/ops/start-aurora-demo.sh stop"
    if ($LASTEXITCODE -ne 0) { throw "Aurora Studio stop command failed (exit $LASTEXITCODE)." }
    Stop-AuroraOwnedListener -Port 8188 -RuntimePath $ComfyUIPath -CommandMarker "ComfyUI\main.py"
    Stop-AuroraOwnedListener -Port 8880 -RuntimePath "C:\AuroraQwenTTS" -CommandMarker "qwen_tts_bridge.py"
    Stop-AuroraOwnedListener -Port 8881 -RuntimePath "C:\AuroraKokoro" -CommandMarker "uvicorn api.src.main:app"
    Write-Host "Stopped Aurora-owned UI, Docker services, ComfyUI, and configured TTS listeners. Shared Ollama was left running."
    exit 0
}

$configuredTtsProvider = (& $wsl.Source -d $Distro -u $LinuxUser -- bash -lc "grep -m1 '^MEDIA_TTS_PROVIDER=' '$ProjectPath/.env' | cut -d= -f2 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'").Trim()
$configuredStoryProvider = (& $wsl.Source -d $Distro -u $LinuxUser -- bash -lc "grep -m1 '^STORY_WORKER_PROVIDER=' '$ProjectPath/.env' | cut -d= -f2 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]'").Trim()
$configuredStoryModel = (& $wsl.Source -d $Distro -u $LinuxUser -- bash -lc "grep -m1 '^STORY_WORKER_MODEL=' '$ProjectPath/.env' | cut -d= -f2 | tr -d '[:space:]'").Trim()

if ($configuredStoryProvider -eq "ollama" -and -not $SkipOllama) {
    $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if (-not $ollama) {
        throw "Aurora is configured for Ollama model '$configuredStoryModel', but ollama.exe is not installed or on PATH. Install Ollama and add the model locally; this launcher will not download models."
    }

    $ollamaReady = $false
    $ollamaModels = $null
    try {
        $ollamaModels = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
        $ollamaReady = $true
    } catch { }

    if (-not $ollamaReady) {
        Write-Host "Starting the existing local Ollama server. The launcher will not download models."
        Assert-AvailablePhysicalMemory -Service "Ollama story model '$configuredStoryModel'" -MinimumGb 3.0
        $env:OLLAMA_HOST = "127.0.0.1:11434"
        $env:OLLAMA_KEEP_ALIVE = "0"
        $env:OLLAMA_MAX_LOADED_MODELS = "1"
        $env:OLLAMA_NUM_PARALLEL = "1"
        $env:OLLAMA_CONTEXT_LENGTH = "4096"
        $ollamaLogRoot = Join-Path $env:LOCALAPPDATA "AuroraStudio\logs"
        New-Item -ItemType Directory -Force -Path $ollamaLogRoot | Out-Null
        Start-Process -FilePath $ollama.Source -ArgumentList @("serve") `
            -WorkingDirectory $env:LOCALAPPDATA `
            -RedirectStandardOutput (Join-Path $ollamaLogRoot "ollama.stdout.log") `
            -RedirectStandardError (Join-Path $ollamaLogRoot "ollama.stderr.log") `
            -WindowStyle Hidden
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 2
            try {
                $ollamaModels = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
                $ollamaReady = $true
                break
            } catch { }
        }
    }

    if (-not $ollamaReady) {
        throw "Ollama did not become healthy at http://127.0.0.1:11434. Inspect $ollamaLogRoot; no model downloads were attempted."
    }
    $modelInstalled = @($ollamaModels.models | ForEach-Object { $_.name }) -contains $configuredStoryModel
    if (-not $modelInstalled) {
        throw "Ollama is running, but configured model '$configuredStoryModel' is not installed. This launcher will not download it."
    }
    Write-Host "Ollama is healthy and model '$configuredStoryModel' is installed."
} elseif ($configuredStoryProvider -ne "ollama") {
    Write-Host "Story provider '$configuredStoryProvider' is selected; the launcher will not start Ollama."
}

if (-not $SkipComfyUI) {
    $comfyPython = Join-Path $ComfyUIPath "python_embeded\python.exe"
    $comfyMain = Join-Path $ComfyUIPath "ComfyUI\main.py"
    $comfyCheckpoint = Join-Path $ComfyUIPath "ComfyUI\models\checkpoints\v1-5-pruned-emaonly-fp16.safetensors"
    if (-not (Test-Path $comfyPython) -or -not (Test-Path $comfyMain)) {
        throw "ComfyUI Portable was not found under '$ComfyUIPath'. Aurora is configured to use ComfyUI; install it there or pass -ComfyUIPath. No model downloads were attempted."
    }
    if (-not (Test-Path $comfyCheckpoint)) {
        throw "The configured local ComfyUI workflow needs the existing SD 1.5 checkpoint at '$comfyCheckpoint'. No model downloads were attempted."
    }

    $wslHostIp = (& $wsl.Source -d $Distro -u $LinuxUser -- bash -lc "ip -4 route show default | cut -d' ' -f3 | head -n1").Trim()
    if ($wslHostIp -notmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}$') {
        throw "Could not determine the Windows host IP for ComfyUI from WSL distribution '$Distro'."
    }
    $comfyUrl = "http://${wslHostIp}:8188"
    $comfyReady = $false
    try {
        $null = Invoke-RestMethod -Uri "$comfyUrl/system_stats" -TimeoutSec 3
        $comfyReady = $true
        Write-Host "ComfyUI already responds at $comfyUrl."
    } catch { }

    if (-not $comfyReady) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort 8188 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener) {
            throw "Port 8188 is in use, but ComfyUI is not reachable from WSL at $comfyUrl. Close or rebind the existing listener, then retry."
        }
        Write-Host "Starting ComfyUI on the WSL host interface with the installed checkpoint. No models will be downloaded."
        Assert-AvailablePhysicalMemory -Service "ComfyUI SD 1.5" -MinimumGb 3.0
        Start-Process -FilePath $comfyPython -ArgumentList @(
            "-s", "ComfyUI\main.py", "--windows-standalone-build", "--fp32-vae", "--fp32-text-enc",
            "--lowvram", "--disable-dynamic-vram", "--listen", $wslHostIp
        ) -WorkingDirectory $ComfyUIPath -RedirectStandardOutput (Join-Path $ComfyUIPath "aurora-comfyui.log") -RedirectStandardError (Join-Path $ComfyUIPath "aurora-comfyui.err.log") -WindowStyle Hidden

        for ($i = 0; $i -lt 120; $i++) {
            Start-Sleep -Seconds 5
            try {
                $null = Invoke-RestMethod -Uri "$comfyUrl/system_stats" -TimeoutSec 3
                $comfyReady = $true
                break
            } catch { }
        }
        if (-not $comfyReady) {
            throw "ComfyUI did not become ready within 10 minutes. Inspect $ComfyUIPath\aurora-comfyui.err.log. No model downloads were attempted."
        }
        Write-Host "ComfyUI is healthy at $comfyUrl."
    }
}

$qwenScript = Join-Path $PSScriptRoot "start_qwen_tts_bridge.ps1"
$qwenRuntime = "C:\AuroraQwenTTS"
$qwenPython = Join-Path $qwenRuntime "venv\Scripts\python.exe"
$qwenCache = Join-Path $qwenRuntime "hf-cache"
$qwenModelCache = Join-Path $qwenCache "hub\models--Qwen--Qwen3-TTS-12Hz-0.6B-CustomVoice"
$qwenWeights = @()
if (Test-Path $qwenModelCache) {
    $qwenWeights = @(Get-ChildItem -Path $qwenModelCache -Filter "*.safetensors" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1)
}
if ($configuredTtsProvider -eq "qwen3_tts" -and -not $SkipQwenTts -and (Test-Path $qwenPython) -and $qwenWeights.Count -gt 0) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8880/health" -TimeoutSec 3
        Write-Host "Qwen-TTS bridge already responds at http://127.0.0.1:8880."
    } catch {
        if (-not (Test-Path $qwenScript)) {
            Write-Warning "Qwen-TTS runtime exists, but its start script is not visible at $qwenScript. Continuing; media may use configured fallback behavior."
        } else {
            Write-Host "Starting the existing Qwen-TTS runtime (no package/model downloads). Model initialization can take several minutes."
            Assert-AvailablePhysicalMemory -Service "Qwen-TTS" -MinimumGb 3.5
            $env:HF_HUB_OFFLINE = "1"
            $env:TRANSFORMERS_OFFLINE = "1"
            Start-Process -FilePath "powershell.exe" -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$qwenScript`"", "-Background", "-Device", "auto", "-Offline"
            ) -WindowStyle Hidden
            $ready = $false
            for ($i = 0; $i -lt 120; $i++) {
                Start-Sleep -Seconds 5
                try {
                    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8880/health" -TimeoutSec 3
                    $ready = $true
                    break
                } catch { }
            }
            if ($ready) { Write-Host "Qwen-TTS bridge is healthy." }
            else { Write-Warning "Qwen-TTS did not become healthy within 10 minutes. No download was attempted; inspect the bridge logs in $qwenRuntime." }
        }
    }
} elseif ($configuredTtsProvider -eq "qwen3_tts" -and -not $SkipQwenTts) {
    Write-Warning "Existing Qwen-TTS environment or cached model weights were not found under $qwenRuntime. Skipping it to protect disk space; the launcher will not install packages or download model weights."
} elseif ($configuredTtsProvider -eq "kokoro" -and -not $SkipKokoroTts) {
    $kokoroScript = Join-Path $PSScriptRoot "start_kokoro_tts.ps1"
    $kokoroRuntime = "C:\AuroraKokoro"
    $kokoroPython = Join-Path $kokoroRuntime ".venv\Scripts\python.exe"
    $kokoroModel = Join-Path $kokoroRuntime "api\src\models\v1_0\kokoro-v1_0.pth"
    if ((Test-Path $kokoroScript) -and (Test-Path $kokoroPython) -and (Test-Path $kokoroModel)) {
        $kokoroReady = $false
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8881/health" -TimeoutSec 3
            $kokoroReady = ($health.status -eq "healthy")
        } catch { }
        if (-not $kokoroReady) {
            $listener = Get-NetTCPConnection -State Listen -LocalPort 8881 -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($listener) {
                throw "Port 8881 is already in use, but Kokoro did not answer its health check. Stop the other service or select its matching MEDIA_TTS_PROVIDER."
            }
            Write-Host "Starting the existing Kokoro CPU runtime (no package/model downloads). First model warm-up may take a few minutes."
            Assert-AvailablePhysicalMemory -Service "Kokoro TTS" -MinimumGb 2.5
            Start-Process -FilePath "powershell.exe" -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$kokoroScript`"", "-Background"
            ) -WindowStyle Hidden
            for ($i = 0; $i -lt 120; $i++) {
                Start-Sleep -Seconds 5
                try {
                    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8881/health" -TimeoutSec 3
                    $kokoroReady = ($health.status -eq "healthy")
                    if ($kokoroReady) { break }
                } catch { }
            }
            if (-not $kokoroReady) {
                throw "Kokoro did not become healthy within 10 minutes. Inspect $kokoroRuntime\aurora-logs\kokoro.stderr.log. No downloads were attempted."
            }
        }
        Write-Host "Kokoro TTS is healthy at http://127.0.0.1:8881."
    } else {
        throw "Kokoro was selected in MEDIA_TTS_PROVIDER, but its existing runtime or downloaded model was not found under $kokoroRuntime. No installation or download was attempted."
    }
} elseif ($configuredTtsProvider -notin @("qwen3_tts", "kokoro")) {
    Write-Host "MEDIA_TTS_PROVIDER='$configuredTtsProvider'; this launcher will not start a TTS service for that provider."
}

& $wsl.Source -d $Distro -u $LinuxUser -- bash -lc "cd '$ProjectPath' && AURORA_WINDOWS_ORCHESTRATED=1 ./scripts/ops/start-aurora-demo.sh start"
if ($LASTEXITCODE -ne 0) { throw "Aurora Studio start command failed (exit $LASTEXITCODE)." }
Start-Process "http://localhost:5173"
