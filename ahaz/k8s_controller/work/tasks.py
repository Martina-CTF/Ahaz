from .. import certmanager, controller
from ..db.models.team import Team
from ..db.operator import set_range


async def gen_cert(team_id: str, port: int, public_domainname: str, certdir: str):
    await certmanager.gen_team(team_id, public_domainname, port, "tcp", certdir)


def create_namespace(team_id: str):
    controller.create_team_namespace(team_id)


async def create_vpn_container(team_id: str):
    await controller.create_team_vpn_container(team_id)


def expose_vpn_container(team_id: str, port: int):
    controller.expose_team_vpn_container(team_id, port)


async def insert_db(team_id: str, port: int):
    team = Team(team_id=team_id, port=port)
    await set_range(team)


async def register_user(team_id: str, user_id: str):
    await controller.register_user_ovpn(team_id, user_id)
