from __future__ import annotations

import os
import webbrowser

from rich.prompt import Prompt

from raincurve.config import load_global_config, save_global_config
from raincurve.ui.console import rc_error, rc_print, rc_success

PROVIDER_URLS = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "openrouter": "https://openrouter.ai/keys",
}

PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5-20250929",
    "openai": "gpt-4o",
    "openrouter": "moonshotai/kimi-k2.5",
}

OPENROUTER_DEFAULT_KEY = os.environ.get("RAINCURVE_OPENROUTER_KEY", "")


def init_llm() -> bool:
    rc_print("\n  LLM provider:")
    rc_print("    1. OpenRouter (recommended — GPT-OSS-120B via Vertex)")
    rc_print("    2. Anthropic (Claude)")
    rc_print("    3. OpenAI")
    choice = Prompt.ask("  Choice", choices=["1", "2", "3"], default="1")

    if choice == "1":
        provider = "openrouter"
    elif choice == "2":
        provider = "anthropic"
    else:
        provider = "openai"

    cfg = load_global_config()

    if provider == "openrouter":
        rc_print("\n  OpenRouter API key:")
        rc_print("    Press Enter to use the built-in key, or paste your own.", style="rc.dim")
        api_key = Prompt.ask("  API key (blank = built-in)", password=True, default="")
        if not api_key:
            api_key = OPENROUTER_DEFAULT_KEY
            rc_print("  Using built-in OpenRouter key.", style="rc.dim")

        model_choice = Prompt.ask(
            "  Model",
            default=DEFAULT_MODELS["openrouter"],
        )

        os.environ["OPENROUTER_API_KEY"] = api_key
        cfg.llm.provider = "openrouter"
        cfg.llm.model = model_choice
        cfg.llm.openrouter_api_key = api_key
        cfg.llm.openrouter_model = model_choice
        save_global_config(cfg)
        rc_success(f"Configured OpenRouter ({model_choice})")
        return True

    url = PROVIDER_URLS[provider]
    rc_print(f"\n  Opening {url}")
    rc_print("  Create or copy your API key, then paste it below.", style="rc.dim")
    webbrowser.open(url)

    api_key = Prompt.ask("  API key", password=True)
    if not api_key:
        rc_error("No API key provided.")
        return False

    os.environ[PROVIDER_ENV_VARS[provider]] = api_key

    cfg.llm.provider = provider
    cfg.llm.model = DEFAULT_MODELS[provider]
    cfg.llm.api_key = api_key
    save_global_config(cfg)

    rc_success(f"Configured {provider} ({DEFAULT_MODELS[provider]})")
    return True


def ensure_llm_key() -> bool:
    cfg = load_global_config()

    # 1. Check env vars first
    for provider in ["openrouter", "anthropic", "openai"]:
        env_var = PROVIDER_ENV_VARS[provider]
        if os.environ.get(env_var):
            if not cfg.llm.provider:
                cfg.llm.provider = provider
                cfg.llm.model = DEFAULT_MODELS[provider]
                save_global_config(cfg)
            return True

    # 2. Load from saved config
    if cfg.llm.provider == "openrouter":
        key = cfg.llm.openrouter_api_key or OPENROUTER_DEFAULT_KEY
        os.environ["OPENROUTER_API_KEY"] = key
        return True

    if cfg.llm.api_key and cfg.llm.provider:
        env_var = PROVIDER_ENV_VARS.get(cfg.llm.provider)
        if env_var:
            os.environ[env_var] = cfg.llm.api_key
            return True

    # 3. Fall back to built-in OpenRouter key
    os.environ["OPENROUTER_API_KEY"] = OPENROUTER_DEFAULT_KEY
    cfg.llm.provider = "openrouter"
    cfg.llm.model = DEFAULT_MODELS["openrouter"]
    cfg.llm.openrouter_api_key = OPENROUTER_DEFAULT_KEY
    cfg.llm.openrouter_model = DEFAULT_MODELS["openrouter"]
    save_global_config(cfg)
    return True
