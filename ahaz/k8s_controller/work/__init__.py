import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis

MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "5"))
LEASE_SECONDS = int(os.getenv("WORKER_LEASE_SECONDS", "30"))
WORK_QUEUE = os.getenv("WORK_QUEUE_KEY", "work:runnable")

logger = logging.getLogger()


class Work:
    id: str
    type: str
    payload: dict
    deps: list[str]
    idempotent_on: dict | None

    def __init__(
        self,
        id: str,
        type: str,
        payload: dict,
        deps: list[str] | None = None,
        idempotent_on: dict | None = None,
    ):
        self.id = id
        self.type = type
        self.payload = payload
        self.deps = deps or []
        self.idempotent_on = idempotent_on


# Subset of Work that should not be created outside of this module
class DoableWork:
    id: str
    type: str
    payload: dict

    def __init__(self, id: str, type: str, payload: dict):
        self.id = id
        self.type = type
        self.payload = payload


def _has_circular_dependencies(tasks: list[Work]) -> bool:
    task_dict = {task.id: task for task in tasks}

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

        deps = task_dict[node].deps
        if deps is not None:
            for dep in deps:
                if visit(dep):
                    return True

        rec_stack.remove(node)
        return False

    for task in tasks:
        if visit(task.id):
            return True

    return False


def _make_idempotency_key(task_type: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"idempotency:{task_type}:{digest}"


def _remap_ids(tasks: list[Work], id_map: dict[str, str]) -> list[Work]:
    for task in tasks:
        if task.id not in id_map:
            continue
        task.id = id_map[task.id]
        task.deps = [id_map[dep] for dep in task.deps]
    return tasks


class WorkQueue:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis_client = redis_client

    async def idempotency(self, idempotency_key: str, task_id: str) -> str:
        inserted = await self.redis_client.setnx(idempotency_key, task_id)

        if inserted:
            return task_id

        existing_id = await self.redis_client.get(idempotency_key)

        if existing_id is None:
            raise Exception("Unexpected missing idempotency key after setnx")

        return existing_id.decode() if isinstance(existing_id, bytes) else existing_id

    async def enqueue_many(self, tasks: list[Work]) -> list[str]:  # noqa: C901 -- Not that complex tbh
        # Check for unmeetable depenencies (i.e. reference task names that don't exist)
        task_ids = {task.id for task in tasks}
        for task in tasks:
            for dep in task.deps:
                if dep not in task_ids:
                    raise Exception(f"Task {task.id} has unmeetable dependency: {dep}")

        # Check for circular dependencies
        if _has_circular_dependencies(tasks):
            raise Exception("Tasks have circular dependencies")

        # Remap IDs by generating UUIDs
        uuid_map = {task.id: f"{task.id}-{uuid.uuid4()}" for task in tasks}
        tasks = _remap_ids(tasks, uuid_map)

        # Remap IDs by idempotency keys
        idempotent_tasks = {task.id: task.id for task in tasks}  # original -> new ID
        for task in tasks:
            if task.idempotent_on is not None:
                idempotency_key = _make_idempotency_key(task.type, task.idempotent_on)
                existing_id = await self.idempotency(idempotency_key, task.id)
                if existing_id != task.id:
                    idempotent_tasks[task.id] = existing_id

        # Determine which tasks we actually need to create (filter BEFORE remapping IDs)
        tasks_to_create = [
            task for task in tasks if task.idempotent_on is None or idempotent_tasks[task.id] == task.id
        ]

        # Apply remapping (some tasks may be remapped to existing IDs)
        tasks = _remap_ids(tasks_to_create, idempotent_tasks)

        while True:
            try:
                # Atomic on all idempotent tasks, to avoid race conditions
                async with self.redis_client.pipeline() as pipe:
                    for id in idempotent_tasks.values():
                        await pipe.watch(f"task:{id}")  # Watch the tasks that are already in the queue

                    # Remove finished idempotent tasks from dependencies
                    finished_tasks = set()
                    for id in idempotent_tasks.values():
                        state = await self.redis_client.hget(f"task:{id}", "state")
                        if state is None:
                            continue  # Task doesn't exist, will be created in this batch

                        if isinstance(state, bytes):
                            state = state.decode()

                        if state == "done":
                            finished_tasks.add(id)

                    for task in tasks:
                        task.deps = [dep for dep in task.deps if dep not in finished_tasks]

                    # Enqueue tasks
                    async with self.redis_client.pipeline() as pipe:
                        pipe.multi()
                        for task in tasks:
                            await pipe.hset(
                                f"task:{task.id}",
                                mapping={
                                    "state": "pending",
                                    "payload": json.dumps({"type": task.type, **task.payload}),
                                    "deps_remaining": len(task.deps),
                                    "attempts": 0,
                                    "max_attempts": MAX_ATTEMPTS,
                                    "lease_until": 0,
                                    "worker": "",
                                },
                            )

                            # Reverse dependency graph
                            for dep in task.deps:
                                # This function is sad :(
                                await pipe.sadd(f"task:children:{dep}", task.id)

                            # Immediately runnable
                            if not task.deps:
                                await pipe.rpush(WORK_QUEUE, task.id)

                        await pipe.execute()

                        return [task.id for task in tasks] + [x for x in finished_tasks]
            except aioredis.WatchError:
                # Atomic failed because something else modified one of the watched keys, retry
                continue

    async def enqueue(self, task: Work) -> str:
        task.id = f"{task.id}-{uuid.uuid4()}"  # Remap ID by generating a UUID

        if task.idempotent_on is not None:
            idempotency_key = _make_idempotency_key(task.type, task.idempotent_on)
            existing_id = await self.idempotency(idempotency_key, task.id)
            if existing_id != task.id:
                return existing_id

        await self.redis_client.hset(
            f"task:{task.id}",
            mapping={
                "state": "pending",
                "payload": json.dumps({"type": task.type, **task.payload}),
                "deps_remaining": len(task.deps),
                "attempts": 0,
                "max_attempts": MAX_ATTEMPTS,
                "lease_until": 0,
                "worker": "",
            },
        )

        # Reverse dependency graph
        for dep in task.deps:
            # This function is sad :(
            await self.redis_client.sadd(f"task:children:{dep}", task.id)

        # Immediately runnable
        if not task.deps:
            await self.redis_client.rpush(WORK_QUEUE, task.id)

        return task.id

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
                return DoableWork(id=task_id, type=payload.pop("type"), payload=payload)
            except Exception as e:
                logger.error(f"{worker_id}: invalid {task_id}")
                logger.debug(f"Error parsing payload for task {task_id}: {e}")
                await self.mark_failed(task_id, abandon=True)  # This cannot be executed by any worker
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

    async def mark_failed(self, task_id: str, abandon: bool = False) -> None:
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

                    if abandon or next_attempt >= max_attempts:
                        # Max attempts reached. Mark failed and don't retry
                        await pipe.hset(
                            key, mapping={"state": "failed", "lease_until": 0, "attempts": max_attempts}
                        )
                        # Mark children as abandoned recursively, since their dependencies will never be met
                        children = await self.redis_client.smembers(f"task:children:{task_id}")
                        for child_id in children:
                            if isinstance(child_id, bytes):
                                child_id = child_id.decode()
                            # NB: this will not loop forever, as it is guaranteed that there are
                            # no circular dependencies
                            await self.mark_failed(child_id, abandon=True)
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
