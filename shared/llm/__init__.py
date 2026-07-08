"""
shared.llm
=========
The reasoning layer of the AI Ecosystem.

Exposes the multi-provider LLM client, the constitutional prompt registry,
and helpers so any agent can reason with a real LLM brain while remaining
constitutionally transparent (no hidden prompts).

Quick use:
    from shared.llm import get_llm, system_prompt
    llm = get_llm()
    if llm.has_any:
        r = llm.complete(system_prompt("atlas"), "Explain CRISPR in 3 sentences.")
        print(r.text)
    else:
        # honest degradation: fall back to non-LLM heuristics
        ...
"""
from shared.llm.client import (
    LLMClient, LLMResult, get_llm,
    AnthropicProvider, OpenAIProvider, GeminiProvider,
)
from shared.llm.prompts import (
    system_prompt, CONSTITUTIONAL_PREAMBLE, AGENT_SYSTEM_PROMPTS,
    prompt_summarize_evidence, prompt_design_agent, prompt_generate_capability,
    prompt_reflect_on_failure, prompt_route_query,
)

__all__ = [
    "LLMClient", "LLMResult", "get_llm",
    "AnthropicProvider", "OpenAIProvider", "GeminiProvider",
    "system_prompt", "CONSTITUTIONAL_PREAMBLE", "AGENT_SYSTEM_PROMPTS",
    "prompt_summarize_evidence", "prompt_design_agent", "prompt_generate_capability",
    "prompt_reflect_on_failure", "prompt_route_query",
]
