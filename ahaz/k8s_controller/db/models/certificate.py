from typing import TypedDict

from pydantic import BaseModel, field_validator


class CertificateDoc(TypedDict):
    # A serial number is 20 bytes, too big to be stored as an int in MongoDB :<
    serial_number: str
    common_name: str
    cert: str
    private_key: str
    # TODO: Expiration data, to let MongoDB automatically delete expired certs


class Certificate(BaseModel):
    serial_number: int
    common_name: str
    cert: str
    private_key: str

    @field_validator("cert", "private_key")
    @classmethod
    def validate_pem(cls, v: str) -> str:
        if not v.startswith("-----BEGIN") or not v.strip().endswith("-----"):
            raise ValueError("Invalid PEM format")
        return v
