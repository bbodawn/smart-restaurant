"""共享测试 fixtures（unit + contract）。

证据标签约定：
- 本目录用 InMemorySaver 跑 Graph/HITL 的用例为 MOCK TEST
  （checkpoint 为内存实现，验证 interrupt/resume 语义，不经真实 Redis）。
- 依赖 MySQL/Redis 的用例放 tests/live/，默认 skip → NOT RUN。
"""
import asyncio
from collections import defaultdict

import pytest
from langgraph.checkpoint.memory import InMemorySaver


@pytest.fixture
def saver():
    """内存 checkpoint saver：Graph/HITL 组离线运行的 checkpoint 实现（MOCK TEST）。"""
    return InMemorySaver()


@pytest.fixture
def counting_node():
    """返回包装工厂，给每个节点加执行计数（同步/异步皆可）。

    用法：先 patch workflow 模块里的节点名、再 build_graph，
    确保 add_node 拿到的是包装器；计数通过 wrap.counters 读取。
    """
    counters = defaultdict(int)

    def factory(fn):
        if asyncio.iscoroutinefunction(fn):
            async def wrapped(state):
                counters[fn.__name__] += 1
                return await fn(state)
        else:
            def wrapped(state):
                counters[fn.__name__] += 1
                return fn(state)
        wrapped.__name__ = fn.__name__
        return wrapped

    factory.counters = counters
    return factory


# Graph 测试需要计数的节点（不含 purchase_approval_node：它内部触发 interrupt）
NODE_NAMES = (
    "inventory_analysis_node",
    "price_analysis_node",
    "supplier_analysis_node",
    "deterministic_policy_node",
    "purchase_prepare",
    "purchase_agent",
    "agent5_node",
)


@pytest.fixture
def no_llm(monkeypatch):
    """把 agent5_node 内部真实 LLM 替换成立即抛错 → 走 fallback。确保 REVIEW 分支离线可跑。"""
    import app.graph.nodes as nodes

    def _raiser(schema):
        raise RuntimeError("LLM disabled in unit test")

    monkeypatch.setattr(nodes, "get_structured_llm", _raiser)
    return nodes


@pytest.fixture
def counting_graph(monkeypatch, saver, counting_node):
    """用计数包装器重建主图，返回 (graph, counters, saver)。"""
    import app.graph.workflow as wf

    for name in NODE_NAMES:
        monkeypatch.setattr(wf, name, counting_node(getattr(wf, name)))
    graph = wf.build_graph(saver)
    return graph, counting_node.counters, saver
