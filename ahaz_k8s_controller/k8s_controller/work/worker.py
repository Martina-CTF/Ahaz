import asyncio
import json
import logging
import os
import sys

# FIXME: this shit is ass, we need relative imports working
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, TypedDict

import redis.asyncio as aioredis
from work import tasks

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

WORK_QUEUE = "work:runnable"
LEASE_SECONDS = 30

LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=LOGLEVEL,
    format="[%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker")

# Set kubernetes client logging level to INFO to reduce verbosity
logging.getLogger("kubernetes").setLevel(logging.INFO)
logging.getLogger("mysql").setLevel(logging.INFO)


async def do_work(work_type: str, payload: dict[str, Any]) -> None:
    match work_type:
        case "gen_cert":
            tasks.gen_cert(
                payload["team_id"], payload["port"], payload["public_domainname"], payload["certdir"]
            )
        case "create_namespace":
            tasks.create_namespace(payload["team_id"])
        case "create_vpn_container":
            tasks.create_vpn_container(payload["team_id"])
        case "expose_vpn_container":
            tasks.expose_vpn_container(payload["team_id"], payload["port"])
        case "insert_db":
            tasks.insert_db(payload["team_id"], payload["port"])
        case "register_user":
            tasks.register_user(payload["team_id"], payload["user_id"])
        case "insert_user_db":
            tasks.insert_user_db(payload["team_id"], payload["user_id"])
        case _:
            raise Exception(f"Unknown work type: {work_type}")


MAX_ATTEMPTS = 5


class Work(TypedDict):
    payload: dict[str, Any]
    deps: list[str]


def has_circular_dependencies(tasks: dict[str, Work]) -> bool:
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


async def create_tasks(r: aioredis.Redis, tasks: dict[str, Work]) -> None:
    # Check for unmeetable dependencies (dependencies that reference task names that don't exist)
    for name, work in tasks.items():
        for dep in work["deps"]:
            if dep not in tasks:
                raise Exception(f"Task {name} has unmeetable dependency: {dep}")

    # Check for circular dependencies
    if has_circular_dependencies(tasks):
        raise Exception("Tasks have circular dependencies")

    # Generate UUIDs for all tasks first, so we can reference them when setting up dependencies
    task_ids = {name: f"{name}-{uuid.uuid4()}" for name in tasks.keys()}

    # Normalize tasks to use UUIDs instead of task names
    normalized_tasks = {
        task_ids[name]: Work(payload=work["payload"], deps=[task_ids[dep] for dep in work["deps"]])
        for name, work in tasks.items()
    }

    # Make sure to insert tasks with dependencies first
    for task_id, work in normalized_tasks.items():
        if len(work["deps"]) > 0:
            await create_task(r, work["payload"], work["deps"], id=task_id)

    # Now insert tasks without dependencies
    for task_id, work in normalized_tasks.items():
        if len(work["deps"]) == 0:
            await create_task(r, work["payload"], work["deps"], id=task_id)


async def create_task(
    r: aioredis.Redis, payload: dict[str, Any], deps: list[str] | None = None, id: str | None = None
) -> str:
    deps = deps or []

    task_id = str(uuid.uuid4()) if id is None else id

    await r.hset(
        f"task:{task_id}",
        mapping={
            "state": "pending",
            "payload": json.dumps(payload),
            "deps_remaining": len(deps),
            "attempts": 0,
            "max_attempts": MAX_ATTEMPTS,
            "lease_until": 0,
            "worker": "",
        },
    )

    # Reverse dependency graph
    for dep in deps:
        await r.sadd(f"task:children:{dep}", task_id)

    # Immediately runnable
    if not deps:
        await r.rpush(WORK_QUEUE, task_id)

    return task_id


async def claim_task(r: aioredis.Redis, task_id: str, worker_id: str) -> bool:
    key = f"task:{task_id}"

    while True:
        try:
            # Atomic on `key`, required for race condition avoidance
            async with r.pipeline() as pipe:
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


async def complete_task(r: aioredis.Redis, task_id: str) -> None:
    """
    Mark task done and unlock children.
    """
    await r.hset(f"task:{task_id}", mapping={"state": "done", "lease_until": 0})

    await r.zrem("work:leases", task_id)

    children = await r.smembers(f"task:children:{task_id}")

    # Update `deps_remaining` and if it hits zero, push to work queue
    for child_id in children:
        child_key_name = f"task:{child_id}"

        while True:
            try:
                # Atomic on `child_key_name`
                async with r.pipeline() as pipe:
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


async def fail_task(r: aioredis.Redis, task_id: str) -> None:
    key = f"task:{task_id}"

    while True:
        try:
            # Atomic, just in case (even though nothing should be modifying the task right now)
            async with r.pipeline() as pipe:
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


async def _worker_loop(worker_id: str, r: aioredis.Redis) -> None:
    logger.info(f"worker started: {worker_id}")

    while True:
        result = await r.blpop(WORK_QUEUE, timeout=0)

        if result is None:
            continue

        _, task_id = result

        if isinstance(task_id, bytes):
            task_id = task_id.decode()

        claimed = await claim_task(r, task_id, worker_id)

        if not claimed:
            continue

        task = await r.hgetall(f"task:{task_id}")

        payload = task["payload"]
        if isinstance(payload, bytes):
            payload = payload.decode()

        try:
            payload = json.loads(payload)
            work_type: str = payload["type"]
            payload["type"] = None
        except Exception:
            logger.error(f"{worker_id}: invalid {task_id}")
            logger.debug(f"Error parsing payload for task {task_id}:")
            traceback.print_exc()
            await complete_task(r, task_id)  # This cannot be executed by any worker
            continue

        try:
            logger.debug(f"{worker_id}: doing {work_type}")
            await r.publish(
                "ahaz_events",
                json.dumps(
                    {
                        "type": "task",
                        "data": {
                            "state": "started",
                            "id": task_id,
                            "type": work_type,
                        },
                    }
                ),
            )
            await do_work(work_type, payload)
            await r.publish(
                "ahaz_events",
                json.dumps(
                    {
                        "type": "task",
                        "data": {
                            "state": "finished",
                            "id": task_id,
                            "type": work_type,
                        },
                    }
                ),
            )
            logger.debug(f"{worker_id}: done {work_type}")
            await complete_task(r, task_id)

        except Exception:
            logger.error(f"{worker_id}: fail {work_type}")
            logger.debug(f"Error processing task {task_id}:")
            traceback.print_exc()
            await fail_task(r, task_id)


# Requeues abandoned tasks whose leases have expired. Should be run seperately as a singleton.
async def _recovery_loop(r: aioredis.Redis) -> None:  # pyright: ignore[reportUnusedFunction]
    while True:
        now = int(datetime.now(timezone.utc).timestamp())

        expired = await r.zrangebyscore("work:leases", 0, now)

        for task_id in expired:
            if isinstance(task_id, tuple):
                task_id = task_id[0]

            if isinstance(task_id, bytes):
                task_id = task_id.decode()

            if isinstance(task_id, list):
                raise Exception(f"unexpected list task_id: {task_id}")

            key = f"task:{task_id}"

            while True:
                try:
                    # Atomic on `key`
                    async with r.pipeline() as pipe:
                        await pipe.watch(key)

                        task = await pipe.hgetall(key)

                        if not task:
                            await pipe.unwatch()
                            break

                        logger.debug(f"Recovery check for task {task_id}: {task}")

                        state = task["state"]

                        lease_until = float(task.get("lease_until", 0))

                        if state != "running" or lease_until > now:
                            await pipe.unwatch()
                            break

                        logger.debug(
                            f"Reclaiming task {task_id} with expired lease (state: {state}, "
                            + f"lease_until: {lease_until}, now: {now})"
                        )

                        pipe.multi()  # Atomic from here on

                        await pipe.hset(key, mapping={"state": "pending", "lease_until": 0})
                        await pipe.rpush(WORK_QUEUE, task_id)
                        await pipe.zrem("work:leases", task_id)

                        await pipe.execute()
                        break

                except aioredis.WatchError:
                    # Atomic failed because something else modified `key`, retry
                    continue

        await asyncio.sleep(5)  # Check every 5 seconds


if __name__ == "__main__":
    worker_id = str(uuid.uuid4())
    redis_client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=None)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_worker_loop(worker_id, redis_client))
