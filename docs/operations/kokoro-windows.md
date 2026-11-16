# Use the existing Windows Kokoro runtime

Aurora can use Kokoro-FastAPI from the already installed `C:\AuroraKokoro` runtime. The launcher below uses the existing `.venv`, model and voices only; it does not run `uv pip install` or download model files.

Aurora selects Kokoro for this local demo because its installed 82M English model is much smaller than the Qwen3-TTS 0.6B runtime and can run on CPU, preserving the GTX 1650 for ComfyUI. Qwen remains available as an alternative for its broader language and voice options. Kokoro has not yet been benchmarked on this machine through Aurora, so compare actual narration duration and listen for voice quality after its bridge is reachable.

## Select Kokoro for the local demo

In the Aurora `.env`, set these values (preserve unrelated credentials and settings):

```dotenv
MEDIA_TTS_PROVIDER=kokoro
MEDIA_TTS_BASE_URL=http://host.docker.internal:8881/v1
MEDIA_TTS_MODEL=kokoro
MEDIA_TTS_VOICE=af_heart
MEDIA_TTS_VOICES=["af_heart"]
MEDIA_TTS_LANGUAGES=["en-US"]
MEDIA_TTS_RIGHTS_BASIS=licensed
MEDIA_TTS_LICENSE_ID=Apache-2.0
MEDIA_TTS_LICENSE_URI=https://www.apache.org/licenses/LICENSE-2.0
MEDIA_TTS_SOURCE_URI=https://huggingface.co/hexgrad/Kokoro-82M
MEDIA_TTS_ATTRIBUTION=Kokoro-82M by hexgrad; Kokoro-FastAPI by remsky
```

Then in **Channels → Profile & schedule → Voice**, select provider `kokoro` and the installed `af_heart` voice for the channel. The channel profile can override the deployment provider, so both must agree. Restart the local demo afterward so the worker reads the updated `.env`.

Run the main Windows launcher. It reads only `MEDIA_TTS_PROVIDER` from `.env` and starts the matching existing local bridge. To start Kokoro alone, open PowerShell from the repository checkout and run:

```powershell
.\scripts\ops\start_kokoro_tts.ps1 -Background
```

The service binds to Windows loopback at port 8881, leaving Qwen on port 8880 undisturbed. Aurora reaches it through the existing Docker host alias and the media egress proxy, which allowlists this exact local service port. Verify `http://127.0.0.1:8881/health` returns `{"status":"healthy"}` and use **Overview → Verify connections** before starting a production.

## Timing and resource profile

The Kokoro model files reported on this machine total about 0.33 GB. CPU mode keeps speech generation off the GTX 1650, leaving GPU memory for ComfyUI. The speed of this installation has not yet been benchmarked with Aurora's narration length; record the narration stage duration in the production run before comparing it with Qwen.

The latest verified 30-second local production completed with these stage times:

| Stage | Duration | Notes |
| --- | ---: | --- |
| Visuals | 131.7 s | Two scenes; includes the configured ComfyUI and Chromium scene pipeline, not a pure GPU sampling measurement. |
| Narration | 203.9 s | Qwen TTS, CPU/GPU resources shared with the local stack. |
| FFmpeg render | 29.4 s | 1080×1920 H.264 with AAC audio and caption track. |
| Technical QA | 2.1 s | Passed technical checks; rights checks still blocked publication. |

Earlier ComfyUI sampling logs measured about 54–62 seconds per 512×768 SD 1.5 still at 24 steps on the GTX 1650. Three such images therefore need roughly three minutes of sampling after ComfyUI is warm, before HTML composition, audio, rendering, QA or retries. A previous visual-stage total near 13 minutes included a failed attempt and retry. Keep one ComfyUI job active at a time and compare quality before lowering sampling steps. Aurora currently generates stills with ComfyUI; FFmpeg adds camera movement and composes audio/captions into the video.

On this 16 GB host, a live resource snapshot showed about 1.6 GB free system RAM while ComfyUI was active. Do not warm another speech model during a production run unless the launcher preflight passes. The launcher requires 2.5 GB free before starting Kokoro, 3 GB before starting ComfyUI, and 3.5 GB before starting Qwen TTS. These thresholds avoid starting model processes when memory is nearly exhausted; close unused applications or stop unneeded workloads if startup is blocked.
