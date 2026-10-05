"""Claude API 없이 에이전트 루프(도구 호출 → 결과 전달 → 최종 답변)가 도는지 확인한다."""

import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent


class FakeMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return NS(stop_reason="tool_use", content=[
                NS(type="tool_use", id="t1", name="score_quotations", input={"rfq_id": "7000000101"})])
        # 두 번째 호출에서는 직전 메시지에 도구 결과가 들어 있어야 한다
        last = kwargs["messages"][-1]["content"][0]
        assert last["type"] == "tool_result" and last["tool_use_id"] == "t1"
        return NS(stop_reason="end_turn", content=[NS(type="text", text="추천 메모")])


def test_loop_runs_tool_and_returns_text(monkeypatch):
    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: NS(messages=FakeMessages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    audit = {"tool_calls": []}
    assert agent.run_with_claude("7000000101", audit) == "추천 메모"
    assert audit["tool_calls"][0]["tool"] == "score_quotations"
