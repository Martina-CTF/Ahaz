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
from typing import Any

import redis.asyncio as aioredis

from . import WORK_QUEUE, WorkQueue, tasks

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


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


async def _worker_loop(worker_id: str, r: aioredis.Redis) -> None:
    logger.info(f"worker started: {worker_id}")

    queue = WorkQueue(r)

    while True:
        work = await queue.wait(worker_id)

        try:
            logger.debug(f"{worker_id}: doing {work.type}")
            await r.publish(
                "ahaz_events",
                json.dumps(
                    {
                        "type": "work",
                        "data": {
                            "state": "started",
                            "id": work.id,
                            "type": work.type,
                        },
                    }
                ),
            )
            await do_work(work.type, work.payload)
            await r.publish(
                "ahaz_events",
                json.dumps(
                    {
                        "type": "work",
                        "data": {
                            "state": "finished",
                            "id": work.id,
                            "type": work.type,
                        },
                    }
                ),
            )
            logger.debug(f"{worker_id}: done {work.type}")
            await queue.mark_complete(work.id)

        except Exception:
            logger.error(f"{worker_id}: fail {work.type}")
            logger.debug(f"Error processing task {work.id}:")
            traceback.print_exc()
            await queue.mark_failed(work.id)


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

    if len(sys.argv) > 1 and sys.argv[1] == "recovery":
        loop.run_until_complete(_recovery_loop(redis_client))
    else:
        loop.run_until_complete(_worker_loop(worker_id, redis_client))
