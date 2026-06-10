import os
from datetime import datetime
from typing import TypedDict

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPrivateKeyTypes
from pydantic import BaseModel, ConfigDict

# Used for encrypting the private key at-rest. It's not really equivalent to a real secret store,
# but at least makes a pure-DB compromise less useful.
# As if it matters anyways since we can just nuke all the certs and regenerate when it happens.
KEY_STORAGE_SECRET = os.getenv(
    "KEY_STORAGE_SECRET", "ifsomeonereadthisanddecidedtouseitonmydbiwouldsurebeinawholelotoftrouble"
)


class CertificateDoc(TypedDict):
    # A serial number is 20 bytes, too big to be stored as an int in MongoDB :<
    serial_number: bytes
    common_name: str
    valid_until: datetime

    cert: bytes
    private_key: bytes

    revocation_list: bytes | None


class Certificate(BaseModel):
    # The crypto objects aren't really serializable which pydantic does not like.
    # Perhaps this is not the best way.
    # TODO: figure out if this is the best way
    model_config = ConfigDict(arbitrary_types_allowed=True)

    serial_number: int
    common_name: str
    valid_until: datetime

    cert: x509.Certificate
    private_key: CertificateIssuerPrivateKeyTypes

    revocation_list: x509.CertificateRevocationList | None = None  # Defined only for CA certs

    def get_certificate_pem(self) -> str:
        return self.cert.public_bytes(encoding=serialization.Encoding.PEM).decode()

    def get_private_key_pem(self) -> str:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()


def cert_to_doc(cert: Certificate) -> CertificateDoc:
    return CertificateDoc(
        serial_number=cert.serial_number.to_bytes(20, "big"),
        common_name=cert.common_name,
        valid_until=cert.valid_until,
        # Serialize the cert as DER, since that is more efficient space-wise than PEM
        # (and we'll be converting it back to a Certificate object when we read it from the DB anyway)
        cert=cert.cert.public_bytes(encoding=serialization.Encoding.DER),
        # Encrypt the private key before storing it in the DB, for at-rest protection.
        # It's admittedly nominal.
        private_key=cert.private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(KEY_STORAGE_SECRET.encode()),
        ),
        revocation_list=cert.revocation_list.public_bytes(encoding=serialization.Encoding.DER)
        if cert.revocation_list
        else None,
    )


def doc_to_cert(doc: CertificateDoc) -> Certificate:
    private_key = serialization.load_der_private_key(
        doc["private_key"],
        password=KEY_STORAGE_SECRET.encode(),
    )

    if not isinstance(private_key, CertificateIssuerPrivateKeyTypes):
        raise ValueError("Invalid private key type")  # some doofus must've inserted a bad private key

    return Certificate(
        serial_number=int.from_bytes(doc["serial_number"], "big"),
        common_name=doc["common_name"],
        valid_until=doc["valid_until"],
        cert=x509.load_der_x509_certificate(doc["cert"]),
        private_key=private_key,
        revocation_list=x509.load_der_x509_crl(doc["revocation_list"]) if doc["revocation_list"] else None,
    )


# This is mainly so it can be saved to a config
# If there is any other use for /just/ the cert, ig then it shld be rewritten
def extract_public_cert(cert: CertificateDoc) -> str:
    return (
        x509.load_der_x509_certificate(cert["cert"])
        .public_bytes(encoding=serialization.Encoding.PEM)
        .decode()
    )
