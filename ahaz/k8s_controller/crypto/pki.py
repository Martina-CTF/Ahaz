import datetime
import logging
import os
from pathlib import Path

from crypto.certificates import (
    create_CA_certificate,
    create_signed_certificate,
    generate_key,
)
from crypto.fs import read_certificate, read_keypair, read_private_key, write_certificate, write_keypair
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPrivateKeyTypes

from k8s_controller.db.models.certificate import Certificate

from ..db.operator import get_certificate_by_common_name, insert_certificate

PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
CERT_DIR_CONTAINER = os.getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certdir")

logger = logging.getLogger()


def get_team_pki_dir(team_id: str) -> Path:
    directory = Path(CERT_DIR_CONTAINER) / team_id / "pki"

    if not directory.exists():
        logger.info(f"PKI for team {team_id} has not yet been initialized. Initializing...")

        os.makedirs(directory / "private", mode=0o700)
        os.makedirs(directory / "issued")
        os.makedirs(directory / "archive")

        (directory / "revoked.crl").touch()  # Create empty CRL file

    return directory


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


def get_server_pair(team_id: str) -> tuple[CertificateIssuerPrivateKeyTypes, x509.Certificate]:
    directory = get_team_pki_dir(team_id)

    # It usually should be that both don't exist or both exist
    # but we'll check both just in case something broke
    if not (directory / "server.crt").exists() or not (directory / "private" / "server.key").exists():
        logger.info(f"No server certificate/key pair found for team {team_id}, creating...")
        key, cert = mint_certificate(team_id, f"server.{team_id}.{PUBLIC_DOMAINNAME}", server=True)
    else:
        key, cert = read_keypair(directory, "server")

    return key, cert


def get_user_pair(team_id: str, user_id: str) -> tuple[CertificateIssuerPrivateKeyTypes, x509.Certificate]:
    directory = get_team_pki_dir(team_id)

    # It usually should be that both don't exist or both exist
    # but we'll check both just in case something broke
    if (
        not (directory / "issued" / f"{user_id}.crt").exists()
        or not (directory / "private" / f"{user_id}.key").exists()
    ):
        logger.info(f"No certificate/key pair found for user {user_id} in team {team_id}, creating...")
        key, cert = mint_certificate(team_id, f"{user_id}.{team_id}.{PUBLIC_DOMAINNAME}")
    else:
        key, cert = read_keypair(directory, user_id)

    return key, cert


def generate_tls_auth_key(team_id: str) -> str:
    directory = get_team_pki_dir(team_id)

    secret = os.urandom(256)  # 2048-bit random key

    secret_hex = secret.hex()
    # Split the hex string into lines of 64 characters for better readability
    formatted_secret = "\n".join([secret_hex[i : i + 64] for i in range(0, len(secret_hex), 64)])

    # Generate static key file content
    armoured_key_content = (
        f"-----BEGIN OpenVPN Static key V1-----\n{formatted_secret}\n-----END OpenVPN Static key V1-----\n"
    )

    with open(directory / "ta.key", "wb") as f:
        f.write(armoured_key_content.encode())

    return armoured_key_content


def get_tls_auth_key(team_id: str) -> str:
    directory = get_team_pki_dir(team_id)

    if not (directory / "ta.key").exists():
        logger.info(f"No TLS auth key found for team {team_id}, creating...")
        return generate_tls_auth_key(team_id)
    else:
        with open(directory / "ta.key", "r") as f:
            return f.read()
