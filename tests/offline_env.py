"""Provider environment forced empty so offline tests never reach a model.

Tests inherit the developer's environment and ``api`` loads ``.env``.  Any
real DeepSeek/SiliconFlow/OpenAI setting would turn an intended offline path
into a network call, so every offline test patches these names to empty.
"""

OFFLINE_PROVIDER_ENV = {
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_BASE_URL": "",
    "DEEPSEEK_MODEL": "",
    "SILICONFLOW_API_KEY": "",
    "SILICONFLOW_BASE_URL": "",
    "SILICONFLOW_MODEL": "",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "",
    "MODEL_NAME": "",
}
