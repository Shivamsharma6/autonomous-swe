from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError


@dataclass(frozen=True, slots=True)
class RedisStreamRecord:
    stream: str
    stream_id: str
    event_id: UUID
    topic: str
    payload: dict[str, Any]


class RedisStreamsTransport:
    """Disposable Redis Streams transport over PostgreSQL-canonical events."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def publish(self, topic: str, event_id: UUID, payload: dict[str, Any]) -> str:
        value = await self._client.xadd(
            topic,
            {
                "event_id": str(event_id),
                "topic": topic,
                "payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            },
        )
        return _text(value)

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def read(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 1_000,
    ) -> tuple[RedisStreamRecord, ...]:
        response = await self._client.xreadgroup(
            group,
            consumer,
            {stream: ">"},
            count=count,
            block=block_ms,
        )
        return _records(response)

    async def reclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        min_idle: timedelta,
        count: int = 10,
    ) -> tuple[RedisStreamRecord, ...]:
        response = await self._client.xautoclaim(
            stream,
            group,
            consumer,
            min_idle_time=max(0, int(min_idle.total_seconds() * 1_000)),
            start_id="0-0",
            count=count,
        )
        messages = response[1] if response else []
        return _records([(stream, messages)])

    async def acknowledge(self, stream: str, group: str, stream_id: str) -> bool:
        return bool(await self._client.xack(stream, group, stream_id))

    async def trim_before(self, stream: str, cutoff: datetime) -> int:
        aware_cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
        minimum_id = f"{int(aware_cutoff.timestamp() * 1_000)}-0"
        return int(await self._client.xtrim(stream, minid=minimum_id, approximate=False))


def _records(response: Any) -> tuple[RedisStreamRecord, ...]:
    result: list[RedisStreamRecord] = []
    for raw_stream, messages in response:
        stream = _text(raw_stream)
        for raw_id, raw_fields in messages:
            fields = {_text(key): _text(value) for key, value in raw_fields.items()}
            payload = cast(dict[str, Any], json.loads(fields["payload"]))
            result.append(
                RedisStreamRecord(
                    stream=stream,
                    stream_id=_text(raw_id),
                    event_id=UUID(fields["event_id"]),
                    topic=fields.get("topic", stream),
                    payload=payload,
                )
            )
    return tuple(result)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
