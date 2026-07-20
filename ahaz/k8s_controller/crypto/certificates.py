# Rolling your own crypto is ALWAYS a good idea :)

import datetime
import logging
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPrivateKeyTypes
from cryptography.x509.oid import NameOID

logger = logging.getLogger()

KEY_ALGO = os.getenv("KEY_ALGO", "ed25519").lower()  # One of ed25519, rsa, ecdsa


def generate_key() -> CertificateIssuerPrivateKeyTypes:
    if KEY_ALGO == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    elif KEY_ALGO == "rsa":
        return rsa.generate_private_key(public_exponent=65537, key_size=4096)
    elif KEY_ALGO == "ecdsa":
        return ec.generate_private_key(ec.SECP384R1())
    else:
        logger.error(f"Unsupported KEY_ALGO: {KEY_ALGO}")
        raise ValueError(f"Unsupported KEY_ALGO: {KEY_ALGO}")


def create_CA_certificate(key: CertificateIssuerPrivateKeyTypes, cn: str) -> x509.Certificate:
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ahaz"),
        ]
    )

    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(
            key,
            # TODO: Make hashing algorithm configurable; in case we are paranoid and SHA-384 is not enough
            # SHA-384 is widely used in TLS certs, so it's a sensible default
            None if isinstance(key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)) else hashes.SHA384(),
        )
    )


def create_signed_certificate(
    key: CertificateIssuerPrivateKeyTypes,
    ca_key: CertificateIssuerPrivateKeyTypes,
    ca_cert: x509.Certificate,
    cn: str,
    server: bool = False,
) -> x509.Certificate:
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ahaz"),
        ]
    )
    csr = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
        .add_extension(
            # Why do I have to specify all of these? :(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )

    if server:
        csr = csr.add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    else:
        csr = csr.add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )

    return csr.sign(ca_key, hashes.SHA384() if isinstance(ca_key, (rsa.RSAPrivateKey)) else None)
