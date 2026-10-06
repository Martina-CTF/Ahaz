import logging
import os

from jinja2 import Environment, FileSystemLoader
from k8s_controller.db.operator import get_certificate_by_common_name, get_pem_by_common_name, get_team, get_certificates_by_cn_suffix, delete_cert_by_common_name

from ..db.models.certificate import Certificate
from .pki import generate_ca, mint_certificate

logger = logging.getLogger()

PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
TEAM_PORT_RANGE_START = int(os.getenv("TEAM_PORT_RANGE_START", "20000"))
K8S_IP_RANGE = os.getenv("K8S_IP_RANGE", "10.42.0.0/24")

pod_net = K8S_IP_RANGE.split("/")[0]
pod_mask_int = K8S_IP_RANGE.split("/")[1] 
pod_mask = ".".join([str((0xFFFFFFFF << (32 - int(pod_mask_int)) >> i) & 0xFF) for i in [24, 16, 8, 0]])


# Config templates
base_dir = os.path.dirname(os.path.abspath(__file__))
j2env = Environment(loader=FileSystemLoader(os.path.join(base_dir, "templates")))
openvpn_conf = j2env.get_template("server/openvpn.conf.j2")

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

async def get_client_certificate(team_id: str, user_id: str) -> Certificate | None:
    common_name = f"{user_id}.{team_id}.{PUBLIC_DOMAINNAME}"
    return await get_certificate_by_common_name(common_name)

async def get_client_certificate_all(team_id: str) -> dict[str, Certificate]:
    certs = await get_certificates_by_cn_suffix(f".{team_id}.{PUBLIC_DOMAINNAME}")
    cert_map: dict[str, Certificate] = {}
    for cn, cert in certs.items():
        if cn.endswith(f".{team_id}.{PUBLIC_DOMAINNAME}"):
            user_id = cn.split(".")[0]
            if user_id not in ["server", "ca"]:
                cert_map[user_id] = cert

    return cert_map

async def get_ca_pem(team_id: str) -> str | None:
    common_name = f"ca.{team_id}.{PUBLIC_DOMAINNAME}"
    return await get_pem_by_common_name(common_name)

async def get_ca_cert(team_id: str) -> Certificate | None:
    common_name = f"ca.{team_id}.{PUBLIC_DOMAINNAME}"
    return await get_certificate_by_common_name(common_name)

async def get_server_cert(team_id: str) -> Certificate | None:
    common_name = f"server.{team_id}.{PUBLIC_DOMAINNAME}"
    return await get_certificate_by_common_name(common_name)

async def get_client_ovpn_config(
    user_id: str,
    team_id: str,
    ovpn_port: int = 1194,
    ovpn_proto: list[str] | None = None,
    ovpn_extra_client_config: list[str] | None = None,
) -> str | None:
    if ovpn_proto is None:
        ovpn_proto = ["tcp"]

    if ovpn_extra_client_config is None:
        ovpn_extra_client_config = []

    client_cert = await get_client_certificate(team_id, user_id)
    if client_cert is None:
        return None

    ca_pem = await get_ca_pem(team_id)
    if ca_pem is None:
        return None

    team = await get_team(team_id)
    if team is None:
        logger.warning(f"Team {team_id} not found when generating client config for user {user_id}")
        return None

    return client_conf.render(
            cn=f"{user_id}.{team_id}.{PUBLIC_DOMAINNAME}",
            port=ovpn_port,
            protocols=ovpn_proto,
            additional_options=ovpn_extra_client_config,
            key=client_cert.get_private_key_pem(),
            cert=client_cert.get_certificate_pem(),
            ca=ca_pem,
            ta=team.ta_key.hex(),
            pod_network=pod_net,
            pod_network_mask=pod_mask
        )

async def get_server_ovpn_config(
        team_id: str,
        ovpn_port: int = 1194,
        ovpn_proto: list[str] | None = None,
        ovpn_extra_server_config: list[str] | None = None,
        ) -> str:
        if ovpn_proto is None:
            ovpn_proto = ["tcp"]
        if ovpn_extra_server_config is None:
            ovpn_extra_server_config = []
        ca_pem = await get_ca_pem(team_id)
        if ca_pem is None:
            raise ValueError(f"CA PEM not found for team {team_id}")

        server_cert = await get_server_cert(team_id)
        if server_cert is None:
            raise ValueError(f"Server certificate not found for team {team_id}")
            
        team = await get_team(team_id)
        if team is None:
            raise ValueError(f"Team {team_id} not found when generating server config")

        return openvpn_conf.render(
                cn=f"server.{team_id}.{PUBLIC_DOMAINNAME}",
                port=ovpn_port,
                protocols=ovpn_proto,
                additional_options=ovpn_extra_server_config,
                key=server_cert.get_private_key_pem(),
                cert=server_cert.get_certificate_pem(),
                ca=ca_pem,
                ta=team.ta_key.hex()
            )

async def delete_team_certificates(team_id: str) -> None:
    certs = await get_certificates_by_cn_suffix(f".{team_id}.{PUBLIC_DOMAINNAME}")
    for cn in certs.keys():
        await delete_cert_by_common_name(cn)

async def delete_user_certificate(team_id: str, user_id: str) -> None:
    cn = f"{user_id}.{team_id}.{PUBLIC_DOMAINNAME}"
    await delete_cert_by_common_name(cn)

async def generate_user(team_id: str, user_id: str):
    try:
        await mint_certificate(team_id, f"{user_id}.{team_id}.{PUBLIC_DOMAINNAME}", server=False)
    except Exception as e:
        logger.error(f"Failed to create user {user_id} for team {team_id}: {str(e)}")
        raise e
