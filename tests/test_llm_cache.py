import pytest
from langchain_core.messages import AIMessage

from querydoctor.agent import llm as llm_module
from querydoctor.agent.state import IndexCandidate, Proposal
from querydoctor.config import get_settings


class CountingInner:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self.result


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_module, "CACHE_DIR", tmp_path)
    return tmp_path


def test_records_always_and_replays_only_in_demo_mode(cache_dir, monkeypatch):
    inner = CountingInner(AIMessage(content="hello"))
    cached = llm_module.CachedLLM(inner)
    msgs = [("system", "s"), ("human", "h")]

    monkeypatch.setattr(get_settings(), "demo_mode", False)
    assert cached.invoke(msgs).content == "hello"
    assert cached.invoke(msgs).content == "hello"
    assert inner.calls == 2                     # no replay outside demo mode
    assert len(list(cache_dir.glob("*.json"))) == 1

    monkeypatch.setattr(get_settings(), "demo_mode", True)
    assert cached.invoke(msgs).content == "hello"
    assert inner.calls == 2                     # replayed from cache

    cached.invoke([("system", "s"), ("human", "different")])
    assert inner.calls == 3                     # cache miss -> real call


def test_structured_replay_returns_model(cache_dir, monkeypatch):
    proposal = Proposal(candidates=[
        IndexCandidate(sql="CREATE INDEX ON t (a)", rationale="r", targets=["x"])
    ])
    inner = CountingInner(proposal)
    cached = llm_module.CachedLLM(inner, schema=Proposal)

    monkeypatch.setattr(get_settings(), "demo_mode", True)
    first = cached.invoke([("human", "q")])
    second = cached.invoke([("human", "q")])

    assert inner.calls == 1
    assert isinstance(second, Proposal)
    assert second == first
