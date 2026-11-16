# Secure LLM provider transport

Aurora Studio uses explicitly configured providers for story generation and claim verification. OpenAI, OpenRouter, DeepSeek, Hugging Face Inference Providers, Gemini, and Groq use a shared chat-completions transport with canonical HTTPS endpoint validation. Anthropic uses its native Messages API adapter. `ollama` uses Ollama's native `/api/chat` endpoint for local models, including Qwen thinking controls and JSON-schema output. `openai_compatible` remains available for vLLM and operator-managed compatible endpoints. No adapter performs paid-provider fallback or accepts an endpoint chosen by model output or tenant content.

## Configuration

- `STORY_WORKER_ENABLED` is disabled by default. Enable it only when a provider endpoint and model have been deliberately configured.
- `STORY_WORKER_PROVIDER` defaults to `openai_compatible`. For local Ollama, select `ollama`, set `STORY_WORKER_LLM_BASE_URL` to the server origin (for example `http://host.docker.internal:11434`), and set the model ID. The native adapter sends the configured thinking toggle using Ollama's `think` field and uses JSON-schema constrained output.
- To use a named hosted provider, set its identifier, the canonical base URL below, `STORY_WORKER_LLM_API_KEY` to that provider's secret API key, and `STORY_WORKER_MODEL` to an available chat model ID. Startup rejects other hosts or paths.

| Provider | Identifier | Canonical base URL |
| --- | --- | --- |
| OpenAI | `openai` | `https://api.openai.com/v1` |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1` |
| DeepSeek | `deepseek` | `https://api.deepseek.com` |
| Hugging Face Inference Providers | `huggingface_router` | `https://router.huggingface.co/v1` |
| Google Gemini OpenAI compatibility | `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| Groq | `groq` | `https://api.groq.com/openai/v1` |
| Anthropic Claude | `anthropic` | `https://api.anthropic.com/v1` |

OpenRouter exposes a large catalog through one compatible endpoint. Model IDs, features, availability, limits, and prices are determined by the selected provider/model and can change. Model-specific vendor features are not automatically enabled; Aurora sends the bounded chat-completions contract only.

The Anthropic adapter places system and developer instructions in the Messages API's top-level `system` field, maps user/assistant turns, and accounts for usage metadata. It uses Anthropic's `x-api-key` and `anthropic-version` headers, and intentionally omits provider-specific reasoning/tool features until the shared LLM contract supports them.
- `STORY_WORKER_LLM_BASE_URL` must be an HTTP(S) URL without embedded credentials, query parameters, or fragments.
- In `APP_ENV=production`, the application requires **HTTPS for every configured LLM endpoint**, whether or not an API key is configured. Production startup rejects plain HTTP.
- `STORY_WORKER_LLM_API_KEY` is optional for local Ollama deployments. When supplied, it is sent only in the HTTP Authorization header; never put a key in the URL.
- Development and test environments may use HTTP for local Ollama or vLLM endpoints, for example `http://localhost:11434/v1` or a private Docker-network service name.

The production policy is intentionally stricter than merely requiring TLS when a key is present: prompts and generated content can also be sensitive, so all production inference traffic must be encrypted in transit.

## Self-hosted Ollama or vLLM in production

Keep the model server on a private network and do not expose its inference port directly to the public internet. Put a TLS-terminating reverse proxy or private ingress (for example, Caddy, Traefik, or Nginx) in front of it, install a certificate trusted by the Aurora Studio worker, and configure `STORY_WORKER_LLM_BASE_URL` with the proxy's HTTPS origin and API path. Restrict ingress to the worker network and firewall the model server so only the proxy can reach it.

TLS termination at the proxy protects traffic between Aurora Studio and that proxy. If the proxy and model server are on different hosts or cross an untrusted network, also encrypt and authenticate the proxy-to-model-server hop. Do not treat a private IP address or a Docker network as a substitute for TLS across an untrusted boundary.

Store API keys in the deployment's secret facility or protected environment configuration, restrict access to the worker process, rotate keys when staff or infrastructure access changes, and never commit secrets to the repository. For local development, a keyless Ollama endpoint on a developer-controlled loopback or private Docker network is supported.

## Logging and provider behavior

The adapter deliberately converts transport and HTTP failures into sanitized `LLMProviderError` messages without including response bodies, prompts, generated text, or API keys. Do not add request/response body logging, Authorization-header logging, or exception logging that serializes provider request payloads. Operational logs should contain stage/run identifiers, provider name, model identifier, duration, and a sanitized error category only.

The HTTP client does not inherit proxy settings from the process environment. Aurora does not retry through another vendor or automatically switch to a paid inference API; any such application-level change must be an explicit operator configuration and auditable product decision. Aggregators may perform their own model/provider routing according to their service settings, so review those settings and billing behavior as well.

Hosted inference routes prompts and generated content to the selected external provider. Check current model/provider availability and account billing before enabling it; credits and prices can change, and usage can be billable. Aurora does not download model weights or call a hosted provider unless the story worker is explicitly enabled and configured. Keep `STORY_WORKER_ENABLED=false` until a deliberate live inference check is intended.

## Verification

Configuration tests cover:
- Development HTTP for local Ollama.
- Production HTTPS with an API key.
- Production HTTP with an API key being rejected.
- Production HTTP without an API key being rejected.

The application configuration validator enforces the transport policy at startup, before the story worker can send requests.
