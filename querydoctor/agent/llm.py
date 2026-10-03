"""
LLM factory (Groq, temperature 0) with a record/replay response cache.

Every LLM response is recorded to data/llm_cache/<sha256>.json, keyed by
model + output schema + exact prompt messages. With DEMO_MODE=1, a cached
response is replayed instead of calling the API — so a slow, failing or
rate-limited provider cannot break a live demo. Prompts are built from
deterministic plan data, so replays line up run after run.
"""

import hashlib
import json
from functools import lru_cache

from langchain_core.messages import AIMessage

from querydoctor.config import PROJECT_ROOT, get_settings


CACHE_DIR = PROJECT_ROOT / "data" / "llm_cache"


@lru_cache
def _raw_llm():

    settings = get_settings()
    provider = settings.llm_provider.lower()

    if provider == "groq":

        from langchain_groq import ChatGroq

        if settings.groq_api_key is None:
            raise RuntimeError("GROQ_API_KEY is not set (see .env.example)")

        return ChatGroq(
            model=settings.llm_model,
            temperature=0,
            api_key=settings.groq_api_key.get_secret_value(),
            max_retries=2,
            timeout=60,
        )

    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")


def _message_pairs(messages) -> list:
    out = []
    for m in messages:
        if isinstance(m, (tuple, list)):
            out.append([str(m[0]), str(m[1])])
        else:                                   # langchain message objects
            out.append([m.type, str(m.content)])
    return out


class CachedLLM:
    """invoke(messages) with record/replay; returns AIMessage or a model."""

    def __init__(self, inner, schema=None):
        self.inner = inner
        self.schema = schema

    def _key(self, messages) -> str:
        payload = {
            "model": get_settings().llm_model,
            "schema": self.schema.__name__ if self.schema else None,
            "messages": _message_pairs(messages),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()

    def invoke(self, messages):

        path = CACHE_DIR / f"{self._key(messages)}.json"

        if get_settings().demo_mode and path.exists():
            data = json.loads(path.read_text())["response"]
            if self.schema is not None:
                return self.schema.model_validate(data)
            return AIMessage(content=data)

        result = self.inner.invoke(messages)

        response = (result.model_dump() if self.schema is not None
                    else result.content)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": self.schema.__name__ if self.schema else None,
            "messages": _message_pairs(messages),
            "response": response,
        }, indent=1))

        return result


def get_llm() -> CachedLLM:
    """Chat model from LLM_PROVIDER / LLM_MODEL, temperature 0."""
    return CachedLLM(_raw_llm())


def get_structured_llm(schema) -> CachedLLM:
    """
    Pydantic structured output. On Groq gpt-oss, function calling was the
    most reliable and fastest; non-strict json_schema sometimes echoes the
    schema back. Strict json_schema is the fallback.
    """

    llm = _raw_llm()

    structured = llm.with_structured_output(
        schema, method="function_calling"
    ).with_fallbacks([
        llm.with_structured_output(schema, method="json_schema", strict=True)
    ])

    return CachedLLM(structured, schema=schema)
