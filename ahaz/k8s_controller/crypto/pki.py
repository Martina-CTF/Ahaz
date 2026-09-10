import datetime
import logging
import os

from ..db.models.certificate import Certificate
from ..db.operator import get_certificate_by_common_name, insert_certificate
from .certificates import (
    create_CA_certificate,
    create_signed_certificate,
    generate_key,
)

PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
CERT_DIR_CONTAINER = os.getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certdir")

logger = logging.getLogger()


# TODO: The PKI state changes here! Handle accordingly.
async def generate_ca(team_id: str) -> Certificate:
    # Check if there's a CA in the DB already
    try:
        _ = await get_certificate_by_common_name(f"ca.{team_id}.{PUBLIC_DOMAINNAME}")
        logger.warning(f"CA certificate for team {team_id} already exists in the DB, likely rollover")
    except ValueError:
        pass  # All good

    key = generate_key()
    cert = create_CA_certificate(key, f"ca.{team_id}.{PUBLIC_DOMAINNAME}")

    cert_data = Certificate(cert=cert, private_key=key)

    await insert_certificate(cert_data)

    return cert_data


async def get_team_ca(team_id: str) -> Certificate:
    cert = None

    try:
        cert = await get_certificate_by_common_name(f"ca.{team_id}.{PUBLIC_DOMAINNAME}")
    except ValueError:
        logger.info(f"No CA certificate found for team {team_id}, creating...")
        cert = await generate_ca(team_id)

    # Generate a bit before expiry to allow rollover
    if cert.cert.not_valid_after < (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    ):
        logger.warning(f"CA certificate for team {team_id} is close to expiry, regenerating...")
        cert = await generate_ca(team_id)

    return cert


# TODO: The PKI state changes here! Handle accordingly.
async def mint_certificate(
    team_id: str, cn: str, server: bool = False
) -> Certificate:
    ca = await get_team_ca(team_id)
   
    try: 
        _ = get_certificate_by_common_name(cn)
        logger.warning(f"Certificate for {cn} already exists in the DB, likely rollover")
    except ValueError:
        pass # First time

    key = generate_key()
    cert = create_signed_certificate(key, ca.private_key, ca.cert, cn, server=server)

    cert_data = Certificate(cert=cert, private_key=key)

    await insert_certificate(cert_data)

    return cert_data
