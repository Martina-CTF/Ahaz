from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPrivateKeyTypes, PrivateKeyTypes


def read_certificate(path: Path) -> x509.Certificate:
    with open(path, "rb") as f:
        cert_data = f.read()
        cert = x509.load_pem_x509_certificate(cert_data)
    return cert


def write_certificate(cert: x509.Certificate, path: Path) -> None:
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def read_private_key(path: Path) -> CertificateIssuerPrivateKeyTypes:
    with open(path, "rb") as f:
        key_data = f.read()
        key = serialization.load_pem_private_key(key_data, password=None)

    if not isinstance(key, CertificateIssuerPrivateKeyTypes):
        raise ValueError(f"Key at {path} is not a valid private key")

    return key


def write_private_key(key: PrivateKeyTypes, path: Path) -> None:
    with open(path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )


def write_keypair(key: PrivateKeyTypes, cert: x509.Certificate, directory: Path, name: str) -> None:
    write_private_key(key, directory / "private" / f"{name}.key")
    write_certificate(cert, directory / "issued" / f"{name}.crt")


def read_keypair(directory: Path, name: str) -> tuple[CertificateIssuerPrivateKeyTypes, x509.Certificate]:
    key = read_private_key(directory / "private" / f"{name}.key")
    cert = read_certificate(directory / "issued" / f"{name}.crt")
    return key, cert


def read_crl(path: Path) -> x509.CertificateRevocationList:
    with open(path, "rb") as f:
        crl_data = f.read()
        crl = x509.load_pem_x509_crl(crl_data)
    return crl


def write_crl(crl: x509.CertificateRevocationList, path: Path) -> None:
    with open(path, "wb") as f:
        f.write(crl.public_bytes(serialization.Encoding.PEM))
