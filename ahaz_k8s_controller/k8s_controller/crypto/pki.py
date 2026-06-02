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

PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
CERT_DIR_CONTAINER = os.getenv("CERT_DIR_CONTAINER", "/etc/ahaz/certdir")

logger = logging.getLogger()


def init_pki(team_id: str) -> None:
    # TODO: Move towards a database-driven approach where we store PKI data inside of a database instead of
    # a filesystem.
    directory = Path(CERT_DIR_CONTAINER) / team_id

    logger.debug(f"Initializing PKI for {cn} in directory {directory}...")

    # Create directory structure
    os.makedirs(os.path.join(directory, "pki", "private"), exist_ok=True)
    os.makedirs(os.path.join(directory, "pki", "issued"), exist_ok=True)

    # Set perms on pki/private
    os.chmod(os.path.join(directory, "pki", "private"), 0o700)

    # Generate CA key and cert
    # ca_key = generate_key()
    ca_cert = create_CA_certificate(ca_key, f"ca.{cn}")

    # Write CA key and cert to disk
    with open(os.path.join(directory, "pki", "private", "ca.key"), "wb") as f:
        f.write(
            ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    with open(os.path.join(directory, "pki", "ca.crt"), "wb") as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))

    # Generate server cert
    server_key = generate_key()
    # TODO: Differentiate teams, perhaps?
    server_cert = create_signed_certificate(server_key, ca_key, ca_cert, f"server.{cn}", server=True)

    # Write server key and cert to disk
    with open(os.path.join(directory, "pki", "private", "server.key"), "wb") as f:
        f.write(
            server_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    with open(os.path.join(directory, "pki", "issued", "server.crt"), "wb") as f:
        f.write(server_cert.public_bytes(serialization.Encoding.PEM))


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
def generate_ca(team_id: str) -> x509.Certificate:
    directory = get_team_pki_dir(team_id)

    key = generate_key()
    cert = create_CA_certificate(key, f"authority.{team_id}.{PUBLIC_DOMAINNAME}")

    if (directory / "ca.crt").exists():
        logger.warning(f"CA certificate for team {team_id} already exists, backing up old cert to archive...")
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        (directory / "ca.crt").move(directory / "archive" / f"ca_{timestamp}.crt")

    write_certificate(cert, directory / "ca.crt")

    return cert


def get_team_ca(team_id: str) -> x509.Certificate:
    directory = get_team_pki_dir(team_id)

    if not (directory / "ca.crt").exists():
        logger.info(f"No CA certificate found for team {team_id}, creating...")
        cert = generate_ca(team_id)
    else:
        cert = read_certificate(directory / "ca.crt")

    # Generate a bit before expiry to allow rollover
    if cert.not_valid_after < (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)):
        logger.warning(f"CA certificate for team {team_id} is close to expiry, regenerating...")
        cert = generate_ca(team_id)

    return cert


# TODO: The PKI state changes here! Handle accordingly.
def mint_certificate(
    team_id: str, cn: str, server: bool = False
) -> tuple[CertificateIssuerPrivateKeyTypes, x509.Certificate]:
    directory = get_team_pki_dir(team_id)

    ca_cert = get_team_ca(team_id)
    ca_key = read_private_key(directory / "private" / "ca.key")

    if (directory / "issued" / f"{cn}.crt").exists() or (directory / "private" / f"{cn}.key").exists():
        logger.warning(
            (
                f"Certificate/key for {cn} already exists for team {team_id},"
                + " backing up old cert/key to archive..."
            )
        )
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        if (directory / "issued" / f"{cn}.crt").exists():
            (directory / "issued" / f"{cn}.crt").move(directory / "archive" / f"{cn}_{timestamp}.crt")
        if (directory / "private" / f"{cn}.key").exists():
            (directory / "private" / f"{cn}.key").move(directory / "archive" / f"{cn}_{timestamp}.key")

    key = generate_key()
    cert = create_signed_certificate(key, ca_key, ca_cert, cn, server=server)

    write_keypair(key, cert, directory, cn)

    return key, cert


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
