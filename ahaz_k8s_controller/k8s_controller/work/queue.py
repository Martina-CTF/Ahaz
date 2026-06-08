import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, NotRequired, TypedDict

import redis.asyncio as aioredis

MAX_ATTEMPTS = 5
LEASE_SECONDS = 30
WORK_QUEUE = "work:runnable"

logger = logging.getLogger()


class Work(TypedDict):
    payload: dict[str, Any]
    deps: list[str]
    idempotent_on: NotRequired[dict | None]


class DoableWork(TypedDict):
    id: str
    type: str
    payload: dict[str, Any]


def _has_circular_dependencies(tasks: dict[str, Work]) -> bool:
    # Simple DFS to detect cycles in the dependency graph
    visited = set()
    rec_stack = set()

    def visit(node: str) -> bool:
        if node in rec_stack:
            return True
        if node in visited:
            return False

        visited.add(node)
        rec_stack.add(node)

        deps = tasks[node]["deps"]
        if deps is not None:
            for dep in deps:
                if visit(dep):
                    return True

        rec_stack.remove(node)
        return False

    for task_name in tasks.keys():
        if visit(task_name):
            return True

    return False


def _make_idempotency_key(task_type: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"idempotency:{task_type}:{digest}"


class WorkQueue:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis_client = redis_client

    async def enqueue_many(self, tasks: dict[str, Work]) -> list[str]:  # noqa: C901 -- Not that complex tbh
        # Check for unmeetable dependencies (dependencies that reference task names that don't exist)
        for name, work in tasks.items():
            for dep in work["deps"]:
                if dep not in tasks:
                    raise Exception(f"Task {name} has unmeetable dependency: {dep}")

        # Check for circular dependencies
        if _has_circular_dependencies(tasks):
            raise Exception("Tasks have circular dependencies")

        # Generate UUIDs for all tasks first, so we can reference them when setting up dependencies
        task_ids = {name: f"{name}-{uuid.uuid4()}" for name in tasks.keys()}

        # Check for idempotency key collisions before enqueuing any tasks
        for name, work in tasks.items():
            idempotent_on = work.get("idempotent_on")
            if idempotent_on is not None:
                idempotency_key = _make_idempotency_key(name, idempotent_on)
                existing_id = await self.idempotency(idempotency_key, task_ids[name])
                if existing_id is not None:
                    # Use existing task ID if a task with the same idempotency key already exists
                    task_ids[name] = existing_id

        # Normalize tasks to use new IDs
        normalized_tasks = {
            task_ids[name]: Work(
                payload=work["payload"],
                deps=[task_ids[dep] for dep in work["deps"]],
                idempotent_on=work.get("idempotent_on"),
            )
            for name, work in tasks.items()
        }

        # Note that this could be done with a pipe, but it is
        # not necessary to be atomic if we do it in the right order

        id_list = []

        # Make sure to insert tasks with dependencies first
        for task_id, work in normalized_tasks.items():
            if len(work["deps"]) > 0:
                id = await self.enqueue(work, task_id)
                id_list.append(id)

        # Now insert tasks without dependencies
        for task_id, work in normalized_tasks.items():
            if len(work["deps"]) == 0:
                id = await self.enqueue(work, id=task_id)
                id_list.append(id)

        return id_list

    async def idempotency(self, idempotency_key: str, task_id: str) -> str | None:
        while True:
            try:
                async with self.redis_client.pipeline() as pipe:
                    if idempotency_key is not None:
                        await pipe.watch(idempotency_key)
                        existing_id = await pipe.get(idempotency_key)

                        if existing_id is not None:
                            await pipe.unwatch()
                            return existing_id.decode() if isinstance(existing_id, bytes) else existing_id

                        await pipe.hset(idempotency_key, task_id)
                        await pipe.execute()

                    return None
            except aioredis.WatchError:
                continue

    async def enqueue(self, work: Work, id: str | None = None) -> str:
        idempotent_on = work.get("idempotent_on")
        payload = work["payload"]
        deps = work["deps"]

        idempotency_key = None
        if idempotent_on is not None:
            idempotency_key = _make_idempotency_key(payload["type"], idempotent_on)

        task_id = str(uuid.uuid4()) if id is None else id

        if idempotency_key is not None:
            existing_id = await self.idempotency(idempotency_key, id or "")
            if existing_id is not None:
                return existing_id

        deps = deps or []

        await self.redis_client.hset(
            f"task:{task_id}",
            mapping={
                "state": "pending",
                "payload": json.dumps(payload),
                "deps_remaining": len(deps),
                "idempotency_key": idempotency_key or "",
                "attempts": 0,
                "max_attempts": MAX_ATTEMPTS,
                "lease_until": 0,
                "worker": "",
            },
        )

        # Reverse dependency graph
        for dep in deps:
            await self.redis_client.sadd(f"task:children:{dep}", task_id)

        # Immediately runnable
        if not deps:
            await self.redis_client.rpush(WORK_QUEUE, task_id)

        return task_id

    async def claim(self, task_id: str, worker_id: str) -> bool:
        key = f"task:{task_id}"

        while True:
            try:
                # Atomic on `key`, required for race condition avoidance
                async with self.redis_client.pipeline() as pipe:
                    await pipe.watch(key)

                    task = await pipe.hgetall(key)

                    if not task:
                        await pipe.unwatch()
                        return False

                    state = task["state"]
                    lease_until = float(task.get("lease_until", 0))

                    now = int(datetime.now(timezone.utc).timestamp())

                    # Already actively running
                    if state == "running" and lease_until > now:
                        await pipe.unwatch()
                        return False

                    # Not runnable
                    if state not in ("pending", "running"):
                        await pipe.unwatch()
                        return False

                    pipe.multi()  # Atomic from here on

                    await pipe.hset(
                        key,
                        mapping={
                            "state": "running",
                            "lease_until": (now + LEASE_SECONDS),
                            "worker": worker_id,
                        },
                    )

                    await pipe.zadd("work:leases", {task_id: now + LEASE_SECONDS})

                    await pipe.execute()
                    return True

            except aioredis.WatchError:
                # Atomic failed because something else modified `key`, retry
                continue

    async def wait(self, worker_id: str) -> DoableWork:
        while True:
            result = await self.redis_client.blpop(WORK_QUEUE, timeout=0)

            if result is None:
                continue

            _, task_id = result

            if isinstance(task_id, bytes):
                task_id = task_id.decode()

            claimed = await self.claim(task_id, worker_id)

            if not claimed:
                continue

            task = await self.redis_client.hgetall(f"task:{task_id}")

            payload = task["payload"]
            if isinstance(payload, bytes):
                payload = payload.decode()

            try:
                payload = json.loads(payload)
                if not isinstance(payload, dict) or "type" not in payload:
                    raise Exception("Invalid payload format")
                return {"type": payload.pop("type"), "id": task_id, "payload": payload}
            except Exception as e:
                logger.error(f"{worker_id}: invalid {task_id}")
                logger.debug(f"Error parsing payload for task {task_id}: {e}")
                await self.mark_complete(task_id)  # This cannot be executed by any worker
                continue

    async def mark_complete(self, task_id: str) -> None:
        await self.redis_client.hset(f"task:{task_id}", mapping={"state": "done", "lease_until": 0})

        await self.redis_client.zrem("work:leases", task_id)

        children = await self.redis_client.smembers(f"task:children:{task_id}")

        # Update `deps_remaining` and if it hits zero, push to work queue
        for child_id in children:
            child_key_name = f"task:{child_id}"

            while True:
                try:
                    # Atomic on `child_key_name`
                    async with self.redis_client.pipeline() as pipe:
                        await pipe.watch(child_key_name)

                        task = await pipe.hgetall(child_key_name)

                        if not task:
                            await pipe.unwatch()
                            break

                        deps_remaining = int(task["deps_remaining"])

                        state = task["state"]

                        new_count = deps_remaining - 1

                        pipe.multi()  # Atomic from here on

                        await pipe.hset(child_key_name, "deps_remaining", new_count)

                        if new_count == 0 and state == "pending":
                            # NO DEPENDENCIES ANYMORE!!! RUN RIGHT NOW!!!
                            await pipe.rpush(WORK_QUEUE, child_id)

                        await pipe.execute()
                        break

                except aioredis.WatchError:
                    # Atomic failed because something else modified `child_key_name`, retry
                    continue

    async def mark_failed(self, task_id: str) -> None:
        key = f"task:{task_id}"

        while True:
            try:
                # Atomic, just in case (even though nothing should be modifying the task right now)
                async with self.redis_client.pipeline() as pipe:
                    await pipe.watch(key)  # Atomic on `key`

                    task = await pipe.hgetall(key)

                    if not task:
                        await pipe.unwatch()
                        return

                    attempts = int(task["attempts"])
                    max_attempts = int(task["max_attempts"])
                    next_attempt = attempts + 1

                    pipe.multi()  # Atomic from here on

                    await pipe.hset(key, "attempts", next_attempt)

                    if next_attempt >= max_attempts:
                        # Max attempts reached. Mark failed and don't retry
                        await pipe.hset(key, mapping={"state": "failed", "lease_until": 0})
                    else:
                        # Retry immediately
                        await pipe.hset(key, mapping={"state": "pending", "lease_until": 0})
                        await pipe.rpush(WORK_QUEUE, task_id)

                    # Remove any existing lease
                    await pipe.zrem("work:leases", task_id)

                    await pipe.execute()
                    return

            except aioredis.WatchError:
                # Atomic failed because something else modified `key`, retry
                continue
