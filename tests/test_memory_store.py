import asyncio
import math

import pytest

from src.config import Settings
from src.memory_store import MemoryStore, level_expiry, _keyword_tokens

run = asyncio.run

TTLS = [2.0, 4.0, 7.0, 14.0, 30.0, 60.0, 120.0, 240.0, 365.0]


def make_store(tmp_path) -> MemoryStore:
    settings = Settings(_env_file=None, db_path=str(tmp_path / "test.db"))
    store = MemoryStore(settings)
    run(store.connect())
    return store


def unit(x: float, y: float, z: float, w: float) -> list[float]:
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    return [x / norm, y / norm, z / norm, w / norm]


def test_level_expiry_gradient():
    assert level_expiry(10, TTLS) is None
    now = "2026-07-16T00:00:00+00:00"
    assert level_expiry(1, TTLS, now) == "2026-07-18T00:00:00+00:00"
    assert level_expiry(9, TTLS, now) == "2027-07-16T00:00:00+00:00"


def test_keyword_tokens_mixed_language():
    tokens = _keyword_tokens("帮我看看 suzuka 项目的构建，还有猫粮的事")
    assert "suzuka" in tokens
    assert any("猫粮" in token for token in tokens)


def test_save_message_and_history(tmp_path):
    store = make_store(tmp_path)
    run(store.save_message("u1", "c1", "user", "你好"))
    run(store.save_message("u1", "c1", "assistant", "你好呀"))
    history = run(store.get_history("u1", "c1", 10))
    assert [m["role"] for m in history] == ["user", "assistant"]
    with pytest.raises(ValueError):
        run(store.save_message("u2", "c1", "user", "偷看"))
    run(store.close())


def test_create_search_and_level_weighting(tmp_path):
    store = make_store(tmp_path)
    cat = run(
        store.create_memory(
            user_id="u1", text="用户养了一只叫年糕的猫", kind="fact", level=8,
            entities=[{"name": "年糕", "type": "pet"}], embedding=unit(1, 0, 0, 0),
            source="test",
        )
    )
    assert cat["level"] == 8 and cat["deduplicated"] is False
    run(
        store.create_memory(
            user_id="u1", text="用户最近在追一部剧", kind="event", level=2,
            entities=[], embedding=unit(0, 1, 0, 0), source="test",
        )
    )
    results = run(store.search_memories("u1", unit(1, 0.2, 0, 0), query_text="猫怎么样了"))
    assert results[0]["text"].startswith("用户养了一只")
    assert results[0]["score"] > 0.9
    # 其他用户完全隔离
    assert run(store.search_memories("u2", unit(1, 0, 0, 0))) == []
    run(store.close())


def test_fingerprint_dedupe_bumps_level_and_renews(tmp_path):
    store = make_store(tmp_path)
    first = run(
        store.create_memory(
            user_id="u1", text="用户在学日语", kind="goal", level=3,
            entities=[], embedding=unit(0, 0, 1, 0), source="test",
        )
    )
    again = run(
        store.create_memory(
            user_id="u1", text="用户在学日语", kind="goal", level=6,
            entities=[], embedding=unit(0, 0, 1, 0), source="test",
        )
    )
    assert again["deduplicated"] is True
    assert again["id"] == first["id"]
    assert again["level"] == 6
    run(store.close())


def test_near_duplicate_vector_merges(tmp_path):
    store = make_store(tmp_path)
    first = run(
        store.create_memory(
            user_id="u1", text="用户喜欢喝美式咖啡", kind="preference", level=5,
            entities=[], embedding=unit(1, 0.001, 0, 0), source="test",
        )
    )
    merged = run(
        store.create_memory(
            user_id="u1", text="用户喜欢喝美式咖啡。", kind="preference", level=5,
            entities=[], embedding=unit(1, 0.002, 0, 0), source="test",
        )
    )
    assert merged["deduplicated"] is True
    assert merged["id"] == first["id"]
    run(store.close())


def test_expired_memory_hidden_from_search(tmp_path):
    store = make_store(tmp_path)
    created = run(
        store.create_memory(
            user_id="u1", text="用户下周要出差", kind="event", level=1,
            entities=[], embedding=unit(0, 1, 0, 0), source="test",
        )
    )
    with store._lock:
        store._conn.execute(
            "UPDATE memories SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (created["id"],),
        )
        store._conn.commit()
    assert run(store.search_memories("u1", unit(0, 1, 0, 0))) == []
    assert run(store.recent_memories("u1")) == []
    run(store.close())


def test_forget_supersede_and_history(tmp_path):
    store = make_store(tmp_path)
    old = run(
        store.create_memory(
            user_id="u1", text="用户在 A 公司上班", kind="fact", level=7,
            entities=[], embedding=unit(1, 0, 0, 0), source="test",
        )
    )
    new = run(
        store.supersede_memory(
            user_id="u1", old_memory_id=old["id"], text="用户跳槽到了 B 公司",
            kind="fact", level=7, entities=[], embedding=unit(0.9, 0.1, 0, 0),
        )
    )
    assert new["superseded"] is True
    # 旧记忆停用后不再出现在检索里
    texts = [m["text"] for m in run(store.search_memories("u1", unit(1, 0, 0, 0)))]
    assert texts == ["用户跳槽到了 B 公司"]
    # 从新旧任一端都能取回完整演变链
    history = [m["text"] for m in run(store.memory_history("u1", old["id"]))]
    assert history == ["用户在 A 公司上班", "用户跳槽到了 B 公司"]
    history = [m["text"] for m in run(store.memory_history("u1", new["id"]))]
    assert history == ["用户在 A 公司上班", "用户跳槽到了 B 公司"]
    assert run(store.forget_memory("u1", new["id"])) is True
    assert run(store.forget_memory("u1", new["id"])) is False
    run(store.close())


def test_link_and_graph_snapshot(tmp_path):
    store = make_store(tmp_path)
    a = run(
        store.create_memory(
            user_id="u1", text="用户在准备 N2 考试", kind="goal", level=6,
            entities=[{"name": "JLPT", "type": "exam"}], embedding=unit(1, 0, 0, 0),
            source="test",
        )
    )
    b = run(
        store.create_memory(
            user_id="u1", text="用户在看日剧练听力", kind="event", level=4,
            entities=[], embedding=unit(0, 1, 0, 0), source="test",
        )
    )
    assert run(store.link_memories("u1", b["id"], a["id"], "supports")) is True
    assert run(store.link_memories("u1", b["id"], "missing", "supports")) is False
    graph = run(store.graph_snapshot("u1"))
    node_types = {node["type"] for node in graph["nodes"]}
    relations = {edge["relation"] for edge in graph["edges"]}
    assert node_types == {"memory", "entity"}
    assert relations == {"mentions", "supports"}
    run(store.close())


def test_mood_trend(tmp_path):
    store = make_store(tmp_path)
    run(store.record_mood("u1", "开心", 2, "考试通过"))
    run(store.record_mood("u1", "疲惫", -1))
    trend = run(store.mood_trend("u1", 7))
    assert trend["count"] == 2
    assert trend["latest_label"] == "疲惫"
    assert trend["avg_valence"] == pytest.approx(0.5)
    assert trend["distribution"] == {"开心": 1, "疲惫": 1}
    recent = run(store.recent_moods("u1", 10))
    assert len(recent) == 2
    run(store.close())
