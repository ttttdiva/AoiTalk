"""Public LLM clients, imported lazily so lifecycle tools do not open the DB."""
from importlib import import_module

_EXPORTS = {
    "AgentLLMClient": (".manager", "AgentLLMClient"),
    "create_llm_client": (".manager", "create_llm_client"),
    "CLILLMClient": (".cli_llm_client", "CLILLMClient"),
    "OpenAICompatibleLocalClient": (".openai_compatible_local_engine", "OpenAICompatibleLocalClient"),
    "SGLangClient": (".sglang_engine", "SGLangClient"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value
