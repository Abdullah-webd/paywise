"""Custom MongoDB checkpointer for LangGraph.

Stores all conversation checkpoints (state history) in MongoDB so they
survive server restarts. This replaces MemorySaver which loses everything
when the process restarts.

Uses the same Motor async client as the rest of the app, so no extra
connections are needed.

Compatible with langgraph-checkpoint 2.1.x / langgraph 0.2.x.
"""
from __future__ import annotations

import random
import logging
from typing import Any, AsyncIterator, Iterator, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.memory import WRITES_IDX_MAP

from app.db import get_db

log = logging.getLogger("paywise.checkpointer")

# We store blobs (channel values) inline in the checkpoint doc for simplicity.
# Each blob is stored as a dict entry keyed by channel name in the doc.


class MongoCheckpointSaver(BaseCheckpointSaver[str]):
    """Async MongoDB-backed checkpointer for LangGraph.

    Collection: "checkpoints"

    Document structure:
        thread_id: str
        checkpoint_ns: str
        checkpoint_id: str
        checkpoint: bytes        # serialized checkpoint (without channel_values)
        metadata: bytes          # serialized metadata
        parent_checkpoint_id: str | None
        channel_values: dict      # {channel_name: serialized_value}
        writes: list              # [{task_id, channel, value, task_path}]
    """

    def __init__(self) -> None:
        super().__init__()

    # ------------------------------------------------------------------- helpers

    def _coll(self):
        return get_db().checkpoints

    async def _ensure_index(self):
        c = self._coll()
        await c.create_index(
            [("thread_id", 1), ("checkpoint_ns", 1), ("checkpoint_id", 1)],
            unique=True,
        )
        await c.create_index("thread_id")

    # --------------------------------------------------------- core interface

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")

        query = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}

        if checkpoint_id := get_checkpoint_id(config):
            query["checkpoint_id"] = checkpoint_id
        else:
            # Get the latest checkpoint for this thread
            doc = await self._coll().find_one(
                {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns},
                sort=[("checkpoint_id", -1)],
            )
            if doc:
                query["checkpoint_id"] = doc["checkpoint_id"]
            else:
                return None

        doc = await self._coll().find_one(query)
        if not doc:
            return None

        return self._doc_to_tuple(doc, config)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        raise NotImplementedError("Use async methods only")

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        query: dict[str, Any] = {}
        if config:
            query["thread_id"] = config["configurable"]["thread_id"]
            checkpoint_ns = config["configurable"].get("checkpoint_ns")
            if checkpoint_ns is not None:
                query["checkpoint_ns"] = checkpoint_ns

        if before and (before_id := get_checkpoint_id(before)):
            query["checkpoint_id"] = {"$lt": before_id}

        sort = [("checkpoint_id", -1)]
        cursor = self._coll().find(query, sort=sort)
        if limit:
            cursor = cursor.limit(limit)

        async for doc in cursor:
            metadata = self.serde.loads_typed(doc["metadata"])
            if filter and not all(
                query_value == metadata.get(query_key)
                for query_key, query_value in filter.items()
            ):
                continue
            yield self._doc_to_tuple(doc, {
                "configurable": {
                    "thread_id": doc["thread_id"],
                    "checkpoint_ns": doc["checkpoint_ns"],
                    "checkpoint_id": doc["checkpoint_id"],
                }
            })

    def list(self, *args, **kwargs):
        raise NotImplementedError("Use async methods only")

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        c = checkpoint.copy()
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]

        # Serialize channel values as blobs
        channel_values_serialized = {}
        for k, v in new_versions.items():
            channel_values_serialized[k] = (
                self.serde.dumps_typed(values[k]) if k in values
                else ("empty", b"")
            )

        doc = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint["id"],
            "checkpoint": self.serde.dumps_typed(c),
            "metadata": self.serde.dumps_typed(
                get_checkpoint_metadata(config, metadata)
            ),
            "parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
            "channel_values": channel_values_serialized,
            "writes": [],
        }

        await self._coll().update_one(
            {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns,
             "checkpoint_id": checkpoint["id"]},
            {"$set": doc},
            upsert=True,
        )

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put(self, *args, **kwargs):
        raise NotImplementedError("Use async methods only")

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        doc_key = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
        doc = await self._coll().find_one(doc_key)
        if not doc:
            return

        existing_writes = doc.get("writes", [])

        for idx, (channel, value) in enumerate(writes):
            write_id = (task_id, WRITES_IDX_MAP.get(channel, idx))
            # Check if this write already exists
            already_exists = any(
                w["task_id"] == task_id and w["idx"] == write_id[1]
                for w in existing_writes
            )
            if already_exists:
                continue

            existing_writes.append({
                "task_id": task_id,
                "idx": write_id[1],
                "channel": channel,
                "value": self.serde.dumps_typed(value),
                "task_path": task_path,
            })

        await self._coll().update_one(
            doc_key,
            {"$set": {"writes": existing_writes}},
        )

    def put_writes(self, *args, **kwargs):
        raise NotImplementedError("Use async methods only")

    async def adelete_thread(self, thread_id: str) -> None:
        await self._coll().delete_many({"thread_id": thread_id})

    def delete_thread(self, *args, **kwargs):
        raise NotImplementedError("Use async methods only")

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"

    # ------------------------------------------------------------ internal

    def _doc_to_tuple(self, doc: dict, config: RunnableConfig) -> CheckpointTuple:
        checkpoint: Checkpoint = self.serde.loads_typed(doc["checkpoint"])

        # Load channel values from the blobs stored alongside the checkpoint
        channel_values: dict[str, Any] = {}
        for k, v in doc.get("channel_values", {}).items():
            if v[0] != "empty":
                channel_values[k] = self.serde.loads_typed(v)

        full_checkpoint = {**checkpoint, "channel_values": channel_values}
        metadata = self.serde.loads_typed(doc["metadata"])

        # Reconstruct pending_writes from stored writes
        pending_writes = []
        for w in doc.get("writes", []):
            pending_writes.append((
                w["task_id"],
                w["channel"],
                self.serde.loads_typed(w["value"]),
            ))

        parent_config = None
        if doc.get("parent_checkpoint_id"):
            parent_config = {
                "configurable": {
                    "thread_id": doc["thread_id"],
                    "checkpoint_ns": doc["checkpoint_ns"],
                    "checkpoint_id": doc["parent_checkpoint_id"],
                }
            }

        return CheckpointTuple(
            config=config,
            checkpoint=full_checkpoint,
            metadata=metadata,
            pending_writes=pending_writes,
            parent_config=parent_config,
        )
