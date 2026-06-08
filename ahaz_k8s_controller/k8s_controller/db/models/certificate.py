from typing import TypedDict

from pydantic import BaseModel, field_validator


class CertificateDoc(TypedDict):
    serial_number: int
    common_name: str
    cert: str
    private_key: str


class Certificate(BaseModel):
    serial_number: int
    common_name: str
    cert: str
    private_key: str

    @field_validator("cert", "private_key")
    @classmethod
    def validate_pem(cls, v: str) -> str:
        if not v.startswith("-----BEGIN") or not v.endswith("-----"):
            raise ValueError("Invalid PEM format")
        return v
