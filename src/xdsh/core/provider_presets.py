"""Public endpoint facts from installed DSH RC2 / pi-ai's built-in catalog.

No model is registered, credential loaded, or request sent by listing presets.
Special auth/protocol families are visible but never misrepresented as supported.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    id: str
    engine: str
    url: str
    limitation: str = ""


NATIVE_PROVIDER_PRESETS = (
    ProviderPreset("amazon-bedrock", "", "", "AWS SigV4 / Bedrock"),
    ProviderPreset("ant-ling", "openai", "https://api.ant-ling.com/v1"),
    ProviderPreset("anthropic", "anthropic", "https://api.anthropic.com"),
    ProviderPreset("azure-openai-responses", "", "", "Azure Responses / deployment / API version"),
    ProviderPreset("cerebras", "openai", "https://api.cerebras.ai/v1"),
    ProviderPreset("cloudflare-ai-gateway", "openai", ""),
    ProviderPreset("cloudflare-workers-ai", "openai", ""),
    ProviderPreset("deepseek", "openai", "https://api.deepseek.com"),
    ProviderPreset("fireworks", "openai", "https://api.fireworks.ai/inference/v1"),
    ProviderPreset("github-copilot", "", "", "GitHub OAuth / token exchange"),
    ProviderPreset("google", "gemini", "https://generativelanguage.googleapis.com/v1beta"),
    ProviderPreset("google-vertex", "", "", "Google ADC / project / location"),
    ProviderPreset("groq", "openai", "https://api.groq.com/openai/v1"),
    ProviderPreset("huggingface", "openai", "https://router.huggingface.co/v1"),
    ProviderPreset("kimi-coding", "anthropic", "https://api.kimi.com/coding"),
    ProviderPreset("minimax", "anthropic", "https://api.minimax.io/anthropic"),
    ProviderPreset("minimax-cn", "anthropic", "https://api.minimaxi.com/anthropic"),
    ProviderPreset("mistral", "", "", "Mistral Conversations protocol"),
    ProviderPreset("moonshotai", "openai", "https://api.moonshot.ai/v1"),
    ProviderPreset("moonshotai-cn", "openai", "https://api.moonshot.cn/v1"),
    ProviderPreset("nvidia", "openai", "https://integrate.api.nvidia.com/v1"),
    ProviderPreset(
        "openai",
        "openai",
        "https://api.openai.com/v1",
        "Chat Completions compatible models only; native Responses not bridged",
    ),
    ProviderPreset("openai-codex", "", "", "ChatGPT OAuth / Codex Responses"),
    ProviderPreset("opencode", "openai", "https://opencode.ai/zen/v1"),
    ProviderPreset("opencode-go", "openai", "https://opencode.ai/zen/go/v1"),
    ProviderPreset("openrouter", "openai", "https://openrouter.ai/api/v1"),
    ProviderPreset(
        "qwen-token-plan",
        "openai",
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    ),
    ProviderPreset(
        "qwen-token-plan-cn",
        "openai",
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ),
    ProviderPreset("together", "openai", "https://api.together.ai/v1"),
    ProviderPreset("vercel-ai-gateway", "anthropic", "https://ai-gateway.vercel.sh"),
    ProviderPreset("xai", "openai", "https://api.x.ai/v1"),
    ProviderPreset("xiaomi", "openai", "https://api.xiaomimimo.com/v1"),
    ProviderPreset("xiaomi-token-plan-ams", "openai", "https://token-plan-ams.xiaomimimo.com/v1"),
    ProviderPreset("xiaomi-token-plan-cn", "openai", "https://token-plan-cn.xiaomimimo.com/v1"),
    ProviderPreset("xiaomi-token-plan-sgp", "openai", "https://token-plan-sgp.xiaomimimo.com/v1"),
    ProviderPreset("zai", "openai", "https://api.z.ai/api/coding/paas/v4"),
    ProviderPreset("zai-coding-cn", "openai", "https://open.bigmodel.cn/api/coding/paas/v4"),
)

PROVIDER_PRESETS = (
    *NATIVE_PROVIDER_PRESETS,
    ProviderPreset("volcengine", "openai", "https://ark.cn-beijing.volces.com/api/coding/v3"),
    ProviderPreset("local", "openai", ""),
)
