$ErrorActionPreference = "Stop"

$runtimeRoot = "C:\AuroraQwenTTS"
$venvDirectory = Join-Path $runtimeRoot "venv"
$python = Join-Path $venvDirectory "Scripts\python.exe"
$cacheRoot = Join-Path $env:LOCALAPPDATA "uv\cache"
$pythonRoot = Join-Path $runtimeRoot "python"
$tempRoot = Join-Path $runtimeRoot "tmp"

New-Item -ItemType Directory -Force -Path $runtimeRoot, $pythonRoot, $tempRoot | Out-Null
$env:UV_CACHE_DIR = $cacheRoot
$env:UV_PYTHON_INSTALL_DIR = $pythonRoot
$env:TEMP = $tempRoot
$env:TMP = $tempRoot

& uv python install 3.12
if ($LASTEXITCODE -ne 0) { throw "Could not install the managed Python 3.12 runtime." }

if (-not (Test-Path $python)) {
    & uv venv --python 3.12 --python-preference managed $venvDirectory
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated Qwen-TTS environment." }
}

& uv pip install --python $python qwen-tts
if ($LASTEXITCODE -ne 0) { throw "Could not install qwen-tts and its dependencies." }

& uv pip install --python $python --extra-index-url https://download.pytorch.org/whl/cu132 "torch==2.15.0+cu132" "torchaudio==2.11.1+cu132"
if ($LASTEXITCODE -ne 0) { throw "Could not install CUDA-enabled PyTorch for the GTX 1650." }

& uv pip install --python $python hf_xet
if ($LASTEXITCODE -ne 0) { throw "Could not install Hugging Face's Xet download helper." }

& $python -c "import torch, qwen_tts; print('Qwen-TTS installed; CUDA available:', torch.cuda.is_available()); print('Torch CUDA build:', torch.version.cuda); raise SystemExit(0 if torch.cuda.is_available() else 'CUDA GPU is unavailable to PyTorch')"
if ($LASTEXITCODE -ne 0) { throw "Qwen-TTS runtime verification failed." }

Get-PSDrive -Name C | ForEach-Object { "C: free space: {0:N1} GB" -f ($_.Free / 1GB) }
