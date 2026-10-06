import asyncio
from .. import controller as k8s
from ..crypto import manager as cert
from ..db.models.team import Team
from ..db import operator as db


async def gen_cert(team_id: str):
    await cert.gen_team(team_id)


def create_namespace(team_id: str):
    k8s.create_team_namespace(team_id)


async def create_vpn_container(team_id: str):
    await k8s.create_team_vpn_container(team_id)


def expose_vpn_container(team_id: str, port: int):
    k8s.expose_team_vpn_container(team_id, port)


async def insert_db(team_id: str, port: int):
    ta_key = cert.gen_ta_key()
    team = Team(team_id=team_id, port=port, ta_key=ta_key)
    await db.set_team(team)


async def register_user(team_id: str, user_id: str):
    await cert.generate_user(team_id, user_id)


async def start_challenge(team_id: str, task: str):
    await k8s.start_challenge(team_id, task)


def stop_challenge(team_id: str, task: str):
    k8s.stop_challenge(team_id, task)

async def delete_team_namespace(team_id: str):
    k8s.delete_namespace(team_id)
    counter = 0
    while k8s.check_namespace_exists(team_id) and counter < 60:
        await asyncio.sleep(1)
        counter += 1
    if counter >= 60:
        raise Exception(f"Timeout waiting for namespace {team_id} to be deleted")

async def delete_team_certificates(team_id: str):
    await cert.delete_team_certificates(team_id)

async def delete_user_certificate(team_id: str, user_id: str):
    await cert.delete_user_certificate(team_id, user_id)

async def delete_team_db(team_id: str):
    await db.delete_team(team_id)
