import logging
import os

from jinja2 import Environment, FileSystemLoader
from k8s_controller.db.operator import get_certificate_by_common_name, get_pem_by_common_name, get_team

from .pki import generate_ca, mint_certificate

logger = logging.getLogger()

PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
TEAM_PORT_RANGE_START = int(os.getenv("TEAM_PORT_RANGE_START", "20000"))


# Config templates
base_dir = os.path.dirname(os.path.abspath(__file__))
j2env = Environment(loader=FileSystemLoader(os.path.join(base_dir, "templates")))
openvpn_conf = j2env.get_template("server/openvpn.conf.j2")
ovpn_env = j2env.get_template("server/ovpn.env.j2")

def gen_ta_key() -> bytes:
    return os.urandom(256)  # 2048-bit random key

async def gen_team(team_id: str):
    try:
        await generate_ca(team_id) # Generate CA

        await mint_certificate(team_id, f"server.{team_id}.{PUBLIC_DOMAINNAME}", server=True)
    except Exception as e:
        logger.error(f"Failed to create team {team_id} VPN directory: {str(e)}")
        raise e


client_conf = j2env.get_template("client/client.ovpn.j2")

async def get_client_ovpn_config(
    user_id: str,
    team_id: str,
    ovpn_port: int = 1194,
    ovpn_proto: list[str] | None = None,
    ovpn_extra_client_config: list[str] | None = None,
) -> str:
    if ovpn_proto is None:
        ovpn_proto = ["tcp"]

    if ovpn_extra_client_config is None:
        ovpn_extra_client_config = []

    client_cert = await get_certificate_by_common_name("{user_id}.{team_id}.{PUBLIC_DOMAINNAME}")
    ca_pem = await get_pem_by_common_name(f"ca.{team_id}.{PUBLIC_DOMAINNAME}")

    team = await get_team(team_id)

    return client_conf.render(
            cn=f"{user_id}.{team_id}.{PUBLIC_DOMAINNAME}",
            port=ovpn_port,
            protocols=ovpn_proto,
            additional_options=ovpn_extra_client_config,
            key=client_cert.get_private_key_pem(),
            cert=client_cert.get_certificate_pem(),
            ca=ca_pem,
            ta=team.ta_key.hex()
        )
