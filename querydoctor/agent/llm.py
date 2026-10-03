from functools import lru_cache

from querydoctor.config import get_settings


@lru_cache
def get_llm():
    """Chat model from LLM_PROVIDER / LLM_MODEL, temperature 0."""

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


def get_structured_llm(schema):
    """
    Pydantic structured output. On Groq gpt-oss, function calling was the
    most reliable and fastest; non-strict json_schema sometimes echoes the
    schema back. Strict json_schema is the fallback.
    """

    llm = get_llm()

    return llm.with_structured_output(
        schema, method="function_calling"
    ).with_fallbacks([
        llm.with_structured_output(schema, method="json_schema", strict=True)
    ])
