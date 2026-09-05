"""Dashboard 读取 checkpoint 中 agent5_analysis 的契约。

覆盖：任何 Redis/checkpoint/状态异常都必须安全降级（返回 None），
绝不能让 /dashboard 因 checkpoint 读取失败而 500。
此处用假对象隔离 graph —— 不连真实 Redis（真实链路属 LIVE E2E / NOT RUN）。
"""
from types import SimpleNamespace

from app.api.dashboard import _load_agent5_from_checkpoint


def _req(graph_holder):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(graph=graph_holder)))


class _SnapValues:
    def __init__(self, values):
        self.values = values


class _OkGraph:
    async def aget_state(self, config):
        return _SnapValues({"agent5_analysis": {"summary": "高", "risk_level": "HIGH"}})


class _NoAgetState:
    pass


class _RaisingGraph:
    async def aget_state(self, config):
        raise RuntimeError("redis down")


async def test_empty_thread_id_returns_none():
    assert await _load_agent5_from_checkpoint(_req(_OkGraph()), None) is None
    assert await _load_agent5_from_checkpoint(_req(_OkGraph()), "") is None


async def test_missing_graph_attribute_returns_none():
    """app.state 上根本没有 graph（未初始化）也不抛。"""
    assert await _load_agent5_from_checkpoint(_req(None), "t-1") is None
    # 无 graph 属性
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    assert await _load_agent5_from_checkpoint(req, "t-1") is None


async def test_graph_without_aget_state_returns_none():
    assert await _load_agent5_from_checkpoint(_req(_NoAgetState()), "t-1") is None


async def test_checkpoint_exception_returns_none_not_500():
    """Redis/checkpoint 读取抛异常 → 返回 None（调用方不 500）。"""
    assert await _load_agent5_from_checkpoint(_req(_RaisingGraph()), "t-1") is None


async def test_happy_path_returns_agent5_dict():
    out = await _load_agent5_from_checkpoint(_req(_OkGraph()), "t-1")
    assert out == {"summary": "高", "risk_level": "HIGH"}
