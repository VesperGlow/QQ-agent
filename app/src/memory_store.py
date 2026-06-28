from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from neo4j import AsyncGraphDatabase

from .config import Settings

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def clean_relation(value: str) -> str:
    cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]", "_", value.strip().lower())
    return cleaned[:80] or "related"


class MemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    async def close(self) -> None:
        await self.driver.close()

    async def connect(self, attempts: int = 30) -> None:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await self.driver.verify_connectivity()
                await self.init_schema()
                return
            except Exception as exc:
                last_error = exc
                if attempt == attempts - 1:
                    break
                await asyncio.sleep(min(2 + attempt, 10))
        raise RuntimeError(f"无法连接或初始化 Neo4j：{last_error}") from last_error

    async def ping(self) -> bool:
        try:
            await self.driver.execute_query(
                "RETURN 1 AS ok", database_=self.settings.neo4j_database
            )
            return True
        except Exception:
            return False

    async def init_schema(self) -> None:
        constraints = [
            "CREATE CONSTRAINT user_id IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE",
            "CREATE CONSTRAINT memory_id IF NOT EXISTS FOR (m:Memory) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT conversation_id IF NOT EXISTS FOR (c:Conversation) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT message_id IF NOT EXISTS FOR (m:Message) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.key IS UNIQUE",
        ]
        for query in constraints:
            await self.driver.execute_query(query, database_=self.settings.neo4j_database)
        vector_query = f"""
        CREATE VECTOR INDEX memory_embedding IF NOT EXISTS
        FOR (m:Memory) ON m.embedding
        OPTIONS {{indexConfig: {{
          `vector.dimensions`: {self.settings.embedding_dimensions},
          `vector.similarity_function`: 'cosine'
        }}}}
        """
        await self.driver.execute_query(vector_query, database_=self.settings.neo4j_database)

    async def save_message(
        self, user_id: str, conversation_id: str, role: str, content: str
    ) -> str:
        message_id = str(uuid.uuid4())
        now = utc_now()
        query = """
        MERGE (u:User {id: $user_id})
          ON CREATE SET u.created_at = $now
        MERGE (c:Conversation {id: $conversation_id})
          ON CREATE SET c.created_at = $now, c.user_id = $user_id
        WITH u, c
        WHERE c.user_id = $user_id
        MERGE (u)-[:HAS_CONVERSATION]->(c)
        CREATE (m:Message {
          id: $message_id, role: $role, content: $content, created_at: $now
        })
        MERGE (c)-[:HAS_MESSAGE]->(m)
        SET c.updated_at = $now
        RETURN m.id AS id
        """
        records, _, _ = await self.driver.execute_query(
            query,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            role=role,
            content=content,
            now=now,
            database_=self.settings.neo4j_database,
        )
        if not records:
            raise ValueError("conversation_id 已属于其他用户")
        return message_id

    async def get_history(
        self, user_id: str, conversation_id: str, limit: int
    ) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        query = """
        MATCH (:User {id: $user_id})-[:HAS_CONVERSATION]->
              (:Conversation {id: $conversation_id})-[:HAS_MESSAGE]->(m:Message)
        RETURN m.role AS role, m.content AS content, m.created_at AS created_at
        ORDER BY m.created_at DESC
        LIMIT $limit
        """
        records, _, _ = await self.driver.execute_query(
            query,
            user_id=user_id,
            conversation_id=conversation_id,
            limit=limit,
            database_=self.settings.neo4j_database,
        )
        return [
            {"role": record["role"], "content": record["content"]}
            for record in reversed(records)
            if record["role"] in {"user", "assistant"}
        ]

    async def search_memories(
        self,
        user_id: str,
        embedding: list[float],
        limit: int | None = None,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        limit = limit or self.settings.memory_search_limit
        min_score = self.settings.memory_min_score if min_score is None else min_score
        candidate_limit = min(max(limit * 5, limit), 250)
        query = """
        CALL db.index.vector.queryNodes('memory_embedding', $candidate_limit, $embedding)
        YIELD node, score
        MATCH (u:User {id: $user_id})-[:HAS_MEMORY]->(node)
        WHERE coalesce(node.active, true) = true AND score >= $min_score
        OPTIONAL MATCH (node)-[:MENTIONS]->(e:Entity)
        WITH node, score, collect(DISTINCT {name: e.name, type: e.type}) AS raw_entities
        SET node.access_count = coalesce(node.access_count, 0) + 1,
            node.last_accessed_at = $now
        RETURN node.id AS id, node.text AS text, node.kind AS kind,
               node.importance AS importance, node.created_at AS created_at,
               score, [entity IN raw_entities WHERE entity.name IS NOT NULL] AS entities
        ORDER BY score DESC
        LIMIT $limit
        """
        try:
            records, _, _ = await self.driver.execute_query(
                query,
                candidate_limit=candidate_limit,
                embedding=embedding,
                user_id=user_id,
                min_score=min_score,
                limit=limit,
                now=utc_now(),
                database_=self.settings.neo4j_database,
            )
        except Exception as exc:
            if "POPULATING" in str(exc).upper():
                logger.warning("Vector index is still populating; returning no memories")
                return []
            raise
        return [dict(record) for record in records]

    async def recent_memories(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        query = """
        MATCH (:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory)
        WHERE coalesce(m.active, true) = true
        OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
        WITH m, collect(DISTINCT {name: e.name, type: e.type}) AS raw_entities
        RETURN m.id AS id, m.text AS text, m.kind AS kind,
               m.importance AS importance, m.created_at AS created_at,
               [entity IN raw_entities WHERE entity.name IS NOT NULL] AS entities
        ORDER BY coalesce(m.last_seen_at, m.created_at) DESC
        LIMIT $limit
        """
        records, _, _ = await self.driver.execute_query(
            query,
            user_id=user_id,
            limit=min(max(limit, 1), 100),
            database_=self.settings.neo4j_database,
        )
        return [dict(record) for record in records]

    async def create_memory(
        self,
        *,
        user_id: str,
        text: str,
        kind: str,
        importance: int,
        entities: list[dict[str, str]],
        embedding: list[float],
        source: str,
    ) -> dict[str, Any]:
        text = text.strip()
        fingerprint = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()
        now = utc_now()
        existing_query = """
        MATCH (:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory {fingerprint: $fingerprint})
        WHERE coalesce(m.active, true) = true
        SET m.last_seen_at = $now,
            m.repetitions = coalesce(m.repetitions, 1) + 1,
            m.importance = CASE WHEN m.importance < $importance THEN $importance ELSE m.importance END
        RETURN m.id AS id, m.text AS text, m.kind AS kind,
               m.importance AS importance, m.created_at AS created_at
        LIMIT 1
        """
        records, _, _ = await self.driver.execute_query(
            existing_query,
            user_id=user_id,
            fingerprint=fingerprint,
            importance=importance,
            now=now,
            database_=self.settings.neo4j_database,
        )
        if records:
            result = dict(records[0])
            result["entities"] = entities
            result["deduplicated"] = True
            return result

        # Qwen3-Embedding 支持 Matryoshka 截维。用极高阈值合并近乎完全相同的
        # 规范化表述；默认 0.995，避免把“喜欢 X”和“不喜欢 X”误合并。
        try:
            candidates = await self.search_memories(
                user_id,
                embedding,
                limit=1,
                min_score=self.settings.memory_duplicate_threshold,
            )
        except Exception:
            candidates = []
        if candidates and candidates[0].get("kind") == kind:
            candidate = candidates[0]
            update_query = """
            MATCH (:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory {id: $memory_id})
            SET m.last_seen_at = $now,
                m.repetitions = coalesce(m.repetitions, 1) + 1,
                m.importance = CASE WHEN m.importance < $importance THEN $importance ELSE m.importance END
            RETURN m.id AS id, m.text AS text, m.kind AS kind,
                   m.importance AS importance, m.created_at AS created_at
            """
            records, _, _ = await self.driver.execute_query(
                update_query,
                user_id=user_id,
                memory_id=candidate["id"],
                importance=importance,
                now=now,
                database_=self.settings.neo4j_database,
            )
            if records:
                result = dict(records[0])
                result["entities"] = candidate.get("entities", [])
                result["deduplicated"] = True
                return result

        memory_id = str(uuid.uuid4())
        safe_entities = []
        for entity in entities[:30]:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name", "")).strip()[:200]
            entity_type = str(entity.get("type", "entity")).strip()[:80] or "entity"
            if name:
                safe_entities.append(
                    {
                        "name": name,
                        "type": entity_type,
                        "key": f"{entity_type.casefold()}:{name.casefold()}",
                    }
                )
        query = """
        MERGE (u:User {id: $user_id})
          ON CREATE SET u.created_at = $now
        CREATE (m:Memory {
          id: $memory_id, text: $text, kind: $kind, importance: $importance,
          embedding: $embedding, fingerprint: $fingerprint, source: $source,
          active: true, repetitions: 1, access_count: 0,
          created_at: $now, last_seen_at: $now
        })
        MERGE (u)-[:HAS_MEMORY]->(m)
        WITH m
        UNWIND $entities AS entity
        MERGE (e:Entity {key: entity.key})
          ON CREATE SET e.name = entity.name, e.type = entity.type, e.created_at = $now
        MERGE (m)-[:MENTIONS]->(e)
        RETURN m.id AS id, m.text AS text, m.kind AS kind,
               m.importance AS importance, m.created_at AS created_at
        """
        if safe_entities:
            records, _, _ = await self.driver.execute_query(
                query,
                user_id=user_id,
                memory_id=memory_id,
                text=text,
                kind=kind,
                importance=importance,
                embedding=embedding,
                fingerprint=fingerprint,
                source=source,
                entities=safe_entities,
                now=now,
                database_=self.settings.neo4j_database,
            )
            result = dict(records[0])
        else:
            no_entity_query = """
            MERGE (u:User {id: $user_id})
              ON CREATE SET u.created_at = $now
            CREATE (m:Memory {
              id: $memory_id, text: $text, kind: $kind, importance: $importance,
              embedding: $embedding, fingerprint: $fingerprint, source: $source,
              active: true, repetitions: 1, access_count: 0,
              created_at: $now, last_seen_at: $now
            })
            MERGE (u)-[:HAS_MEMORY]->(m)
            RETURN m.id AS id, m.text AS text, m.kind AS kind,
                   m.importance AS importance, m.created_at AS created_at
            """
            records, _, _ = await self.driver.execute_query(
                no_entity_query,
                user_id=user_id,
                memory_id=memory_id,
                text=text,
                kind=kind,
                importance=importance,
                embedding=embedding,
                fingerprint=fingerprint,
                source=source,
                now=now,
                database_=self.settings.neo4j_database,
            )
            result = dict(records[0])
        result["entities"] = [
            {"name": entity["name"], "type": entity["type"]} for entity in safe_entities
        ]
        result["deduplicated"] = False
        return result

    async def forget_memory(self, user_id: str, memory_id: str) -> bool:
        query = """
        MATCH (:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory {id: $memory_id})
        SET m.active = false, m.forgotten_at = $now
        RETURN count(m) AS changed
        """
        records, _, _ = await self.driver.execute_query(
            query,
            user_id=user_id,
            memory_id=memory_id,
            now=utc_now(),
            database_=self.settings.neo4j_database,
        )
        return bool(records and records[0]["changed"])

    async def link_memories(
        self, user_id: str, from_id: str, to_id: str, relation: str
    ) -> bool:
        query = """
        MATCH (u:User {id: $user_id})-[:HAS_MEMORY]->(a:Memory {id: $from_id})
        MATCH (u)-[:HAS_MEMORY]->(b:Memory {id: $to_id})
        MERGE (a)-[r:RELATED_TO]->(b)
        SET r.kind = $relation, r.updated_at = $now
        RETURN count(r) AS changed
        """
        records, _, _ = await self.driver.execute_query(
            query,
            user_id=user_id,
            from_id=from_id,
            to_id=to_id,
            relation=clean_relation(relation),
            now=utc_now(),
            database_=self.settings.neo4j_database,
        )
        return bool(records and records[0]["changed"])

    async def graph_snapshot(self, user_id: str, limit: int = 100) -> dict[str, Any]:
        query = """
        MATCH (u:User {id: $user_id})-[:HAS_MEMORY]->(m:Memory)
        WHERE coalesce(m.active, true) = true
        WITH u, m ORDER BY m.created_at DESC LIMIT $limit
        OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
        OPTIONAL MATCH (m)-[r:RELATED_TO]->(other:Memory)
        RETURN collect(DISTINCT {
          id: m.id, label: m.text, type: 'memory', kind: m.kind
        }) + collect(DISTINCT {
          id: e.key, label: e.name, type: 'entity', kind: e.type
        }) AS nodes,
        collect(DISTINCT CASE WHEN e IS NULL THEN NULL ELSE {
          source: m.id, target: e.key, relation: 'mentions'
        } END) + collect(DISTINCT CASE WHEN r IS NULL THEN NULL ELSE {
          source: m.id, target: other.id, relation: r.kind
        } END) AS edges
        """
        records, _, _ = await self.driver.execute_query(
            query,
            user_id=user_id,
            limit=min(max(limit, 1), 500),
            database_=self.settings.neo4j_database,
        )
        if not records:
            return {"nodes": [], "edges": []}
        return {
            "nodes": [node for node in records[0]["nodes"] if node.get("id")],
            "edges": [edge for edge in records[0]["edges"] if edge],
        }
