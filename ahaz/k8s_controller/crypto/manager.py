import logging
import os
import subprocess
from shutil import rmtree

import dboperator
from crypto.certificates import (
    create_CA_certificate,
    create_signed_certificate,
    generate_key,
)
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPrivateKeyTypes
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger()

PUBLIC_DOMAINNAME = os.getenv("PUBLIC_DOMAINNAME", "ahaz.lan")
TEAM_PORT_RANGE_START = int(os.getenv("TEAM_PORT_RANGE_START", "20000"))


# Config templates
base_dir = os.path.dirname(os.path.abspath(__file__))
j2env = Environment(loader=FileSystemLoader(os.path.join(base_dir, "templates")))
openvpn_conf = j2env.get_template("server/openvpn.conf.j2")
ovpn_env = j2env.get_template("server/ovpn.env.j2")


def gen_configs_ovpn(teamdirContainer: str, domainname: str, port: int, protocol: str) -> None:
    # {teamdir}/openvpn.conf
    with open(os.path.join(teamdirContainer, "openvpn.conf"), "w") as f:
        f.write(openvpn_conf.render(domainname=domainname, port=port, proto=protocol))
    # {teamdir}/ovpn_env.sh
    with open(os.path.join(teamdirContainer, "ovpn_env.sh"), "w") as f:
        f.write(ovpn_env.render(domainname=domainname, port=port, proto=protocol))


def gen_ta_key(teamdirContainer: str) -> None:
    secret = os.urandom(256)  # 2048-bit random key
    with open(os.path.join(teamdirContainer, "pki", "ta.key"), "wb") as f:
        f.write("-----BEGIN OpenVPN Static key V1-----\n".encode())
        f.write(secret.hex().encode())
        f.write("\n".encode())
        f.write("-----END OpenVPN Static key V1-----\n".encode())


def gen_team(team_name: str, domain: str, port: int, protocol: str, cert_dir_location: str) -> int:
    try:
        teamdirContainer = cert_dir_location + team_name

        logger.debug(f"Creating certificate directory for team {team_name} at {teamdirContainer}...")
        os.makedirs(teamdirContainer)

        logger.debug(f"Creating PKI for team {team_name}...")
        init_pki(teamdirContainer, domain)

        logger.debug(f"Generating OpenVPN configuration files for team {team_name}...")
        gen_configs_ovpn(teamdirContainer, domain, port, protocol)

        logger.debug(f"Generating TLS authentication key for team {team_name}...")
        gen_ta_key(teamdirContainer)

        return 0
    except Exception as e:
        logger.error("Failed to create team " + team_name + " VPN directory: " + str(e))
        raise e


def del_team(team_name: str, cert_dir_location: str) -> None:
    try:
        teamdirContainer = cert_dir_location + team_name

        logger.debug("Deleting team " + team_name + "'s certificate directory...")
        rmtree(teamdirContainer)
    except Exception as e:
        logger.error("Failed to delete container directory for team " + team_name + ": " + str(e))


def get_ca_key(cert_dir: str) -> CertificateIssuerPrivateKeyTypes:
    key_path = os.path.join(cert_dir, "pki", "private", "ca.key")
    with open(key_path, "rb") as f:
        key_data = f.read()
        key = serialization.load_pem_private_key(key_data, password=None)

    if not isinstance(key, CertificateIssuerPrivateKeyTypes):
        raise ValueError("CA key is not a valid private key")

    return key


def get_server_ca(cert_dir: str) -> x509.Certificate:
    ca_path = os.path.join(cert_dir, "pki", "ca.crt")
    with open(ca_path, "rb") as f:
        ca_cert_data = f.read()
        ca_cert = x509.load_pem_x509_certificate(ca_cert_data)

    return ca_cert


def get_server_ta(cert_dir: str) -> str:
    ta_path = os.path.join(cert_dir, "pki", "ta.key")
    with open(ta_path, "r") as f:
        ta_content = f.read()
    return ta_content


def get_server_cert(cert_dir: str) -> x509.Certificate:
    cert_path = os.path.join(cert_dir, "pki", "issued", "server.crt")
    with open(cert_path, "rb") as f:
        cert_data = f.read()
        cert = x509.load_pem_x509_certificate(cert_data)

    return cert


def get_server_key(cert_dir: str) -> CertificateIssuerPrivateKeyTypes:
    key_path = os.path.join(cert_dir, "pki", "private", "server.key")
    with open(key_path, "rb") as f:
        key_data = f.read()
        key = serialization.load_pem_private_key(key_data, password=None)

    if not isinstance(key, CertificateIssuerPrivateKeyTypes):
        raise ValueError("Server key is not a valid private key")

    return key


def get_server_ovpn_config(cert_dir: str) -> str:
    conf_path = os.path.join(cert_dir, "openvpn.conf")
    with open(conf_path, "r") as f:
        conf_content = f.read()
    return conf_content


def get_openvpn_env(cert_dir: str) -> str:
    env_path = os.path.join(cert_dir, "ovpn_env.sh")
    with open(env_path, "r") as f:
        env_content = f.read()
    return env_content


def gen_user(user_name: str, team_cert_dir: str) -> None:
    try:
        # Read CA cert into x509.Certificate object
        ca_cert = get_server_ca(team_cert_dir)

        # Read CA key into appropriate private key object
        ca_key = get_ca_key(team_cert_dir)

        # Generate user key
        user_key = generate_key()

        # Create signed certificate for user
        user_cert = create_signed_certificate(user_key, ca_key, ca_cert, user_name)

        # Write user key and cert to disk
        with open(os.path.join(team_cert_dir, "pki", "private", f"{user_name}.key"), "wb") as f:
            f.write(
                user_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
        with open(os.path.join(team_cert_dir, "pki", "issued", f"{user_name}.crt"), "wb") as f:
            f.write(user_cert.public_bytes(serialization.Encoding.PEM))
    except Exception as e:
        logger.error("Failed to generate certificate for user " + user_name + ": " + str(e))
        raise e


def del_user(team_name: str, user_name: str, team_cert_dir: str) -> None:
    try:
        user_key_path = os.path.join(team_cert_dir, "pki", "private", f"{user_name}.key")
        user_cert_path = os.path.join(team_cert_dir, "issued", f"{user_name}.crt")

        logger.debug(f"Deleting certificate and key for user {user_name} in team {team_name}...")
        os.remove(user_key_path)
        os.remove(user_cert_path)
    except Exception as e:
        logger.error("Failed to delete certificate for user " + user_name + ": " + str(e))
        raise e


client_conf = j2env.get_template("client/client.ovpn.j2")


def get_client_ovpn_config(
    ovpn_cn: str,
    cn: str,
    easyrsa_pki: str,
    ovpn_port: int = 1194,
    ovpn_proto: list[str] | None = None,
    ovpn_extra_client_config: list[str] | None = None,
) -> str:
    if ovpn_proto is None:
        ovpn_proto = ["tcp"]

    if ovpn_extra_client_config is None:
        ovpn_extra_client_config = []

    try:
        key_path = os.path.join(easyrsa_pki, "private", f"{cn}.key")
        cert_path = os.path.join(easyrsa_pki, "issued", f"{cn}.crt")
        ca_path = os.path.join(easyrsa_pki, "ca.crt")
        ta_path = os.path.join(easyrsa_pki, "ta.key")

        with open(key_path, "r") as f:
            key_content = f.read()

        with open(cert_path, "r") as f:
            cert_content = f.read()

        with open(ca_path, "r") as f:
            ca_content = f.read()

        with open(ta_path, "r") as f:
            ta_content = f.read()

        return client_conf.render(
            cn=ovpn_cn,
            port=ovpn_port,
            protocols=ovpn_proto,
            additional_options=ovpn_extra_client_config,
            key=key_content.strip(),
            cert=cert_content.strip(),
            ca=ca_content.strip(),
            ta=ta_content.strip(),
        )
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found at {e.filename}")
        raise e
    except subprocess.CalledProcessError as e:
        logger.error(f"Error executing openssl: {e.stderr}")
        raise e


def get_team_vpn_pod_port(team_id: str) -> int:
    port_resp = dboperator.get_team_port(team_id)
    if port_resp != "null":
        return int(port_resp)
    else:
        return TEAM_PORT_RANGE_START + int(team_id) - 1


def generate_user(team_id: str, user_id: str, team_cert_dir: str) -> str:
    gen_user(user_id, team_cert_dir)

    return get_client_ovpn_config(
        PUBLIC_DOMAINNAME,
        user_id,
        os.path.join(team_cert_dir, "pki"),
        # HACK: make a better way of setting the port the client should connect to
        ovpn_port=get_team_vpn_pod_port(team_id),
    )


def get_user(team_id: str, user_id: str, team_cert_dir: str) -> str:
    return get_client_ovpn_config(
        PUBLIC_DOMAINNAME,
        user_id,
        os.path.join(team_cert_dir, "pki"),
        # HACK: make a better way of setting the port the client should connect to
        ovpn_port=get_team_vpn_pod_port(team_id),
    )
