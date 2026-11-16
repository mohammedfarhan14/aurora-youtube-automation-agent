# Qwen3-TTS local bridge

Aurora calls TTS through an OpenAI-compatible `GET /v1/models` and `POST /v1/audio/speech` interface. Qwen's `qwen-tts-demo` is a Gradio UI, so `scripts/ops/qwen_tts_bridge.py` adapts its Python inference API to the endpoint Aurora already uses. The bridge runs separately from the backend; Torch and model weights do not enter the API or media-worker image.

## Demo model choice

Use `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` and its built-in English `Ryan` or `Aiden` voice for this 4 GB GPU demo. The official model card lists the checkpoint files at about 2.5 GB, and the upstream quickstart recommends a separate Python 3.12 environment. The 1.7B models are too large to be the first choice on a 4 GB GPU. The 0.6B model may still exceed available VRAM once runtime memory is included; `QWEN_TTS_DEVICE=auto` attempts CUDA and falls back to CPU on a CUDA out-of-memory error. CPU synthesis can be slow.

Keep the runtime and Hugging Face cache off a nearly full system drive. Qwen's documented installation and model-loading behavior are in the [upstream README](https://github.com/QwenLM/Qwen3-TTS#quickstart).

## Install and launch

On Windows, from PowerShell at the repository root, install the isolated runtime and launch the bridge with the repository scripts:

```powershell
.\scripts\ops\setup_qwen_tts.ps1
.\scripts\ops\start_qwen_tts_bridge.ps1
```

The scripts keep the virtual environment, temporary files, and model cache under `C:\AuroraQwenTTS`; package downloads reuse the existing uv cache. Confirm the system drive has enough free space before using this setup. The bridge binds to Windows loopback so the local Docker Desktop host alias can reach it without exposing an unauthenticated model service to the LAN. The bridge defaults to CPU inference on this GTX 1650 setup. A clean CUDA retest loaded the checkpoint but produced non-finite waveform samples; the bridge rejects those responses instead of writing corrupted audio. CPU output is validated and peak-normalized before writing WAV. The `-Device auto` and `-Device cuda:0` options remain available to retest another driver/runtime combination. Sub-talker sampling is disabled by default because CUDA sampling produced invalid probabilities; the main talker remains sampled.

## Aurora configuration

In the local `.env`, configure the adapter as follows after the bridge is reachable from the Docker media worker:

```dotenv
MEDIA_TTS_PROVIDER=qwen3_tts
MEDIA_TTS_BASE_URL=http://host.docker.internal:8880/v1
# Compose sets MEDIA_TTS_PROXY_URL for the media worker; it cannot reach public hosts.
# Leave unset while the bridge is loopback-only.
MEDIA_TTS_MODEL=Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
MEDIA_TTS_VOICE=Ryan
MEDIA_TTS_VOICES=["Ryan","Aiden"]
MEDIA_TTS_LANGUAGES=["English"]
MEDIA_TTS_SAMPLE_RATES_HZ=[24000]
MEDIA_TTS_HOSTING_MODE=self_hosted
MEDIA_TTS_RIGHTS_BASIS=licensed
MEDIA_TTS_LICENSE_ID=Apache-2.0
MEDIA_TTS_LICENSE_URI=https://www.apache.org/licenses/LICENSE-2.0
MEDIA_TTS_SOURCE_URI=https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
MEDIA_TTS_ATTRIBUTION=Qwen3-TTS by the Qwen Team
MEDIA_TTS_SPEED=1.0
MEDIA_TTS_TIMEOUT_SECONDS=300
MEDIA_WORKER_ENABLED=true
```

The internal media worker uses `media-egress-proxy` for local Qwen and public HTTPS provider access. The proxy rejects private, loopback, link-local, multicast and reserved destinations; its only non-HTTPS exception is `host.docker.internal:8880`. This broader public HTTPS path is for local demos; hosted deployments need provider-specific hostname allowlists. Set `QWEN_TTS_HOST_GATEWAY_IP` in the ignored local `.env` only if Squid selects the unreachable IPv6 host alias; on this machine the working IPv4 gateway is `192.168.65.254`. Leave it unset elsewhere to use Docker's `host-gateway` value.

The model card identifies the checkpoint as Apache-2.0 licensed. Verify that license and the selected voice's terms before publishing. The bridge only accepts WAV at speed 1.0 because Aurora currently passes speed but not language to the OpenAI-compatible request; English is selected by the bridge. The TTS model does not provide word timings, so Aurora uses its estimated caption timing until a forced-alignment provider is configured.

## Smoke checks

With the bridge running, check `GET http://localhost:8880/health` locally and verify `GET /v1/models` from the Docker media-worker network. The Qwen bridge may take several minutes to synthesize a 30-second script on this CPU, so use the 300-second local TTS timeout. Then run one non-publish test production, review the generated audio/video and rights metadata, and only afterward enable the media worker for the demo. This bridge does not upload anything to YouTube; publishing still requires the existing channel consent and approval workflow.
