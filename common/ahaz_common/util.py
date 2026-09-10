import re

ibi_regex = r"^(\d+)([KMGTP]i)$"
ilo_regex = r"^(\d+)([KMGTP]b?)$"  # Should catch both "Mb" and "M"
dec_regex = r"^\d+$"

powerdict = {"M": 2, "G": 3, "T": 4, "P": 5}


def adapt_limit_size(limit: str) -> str:
    """
    Adapt the limit size to a format that Kubernetes can understand.
    For example, if the limit is "512Mb", it will be converted to "512M".
    If the input is a IEC (i.e. Mi) unit or an integer (which represents bytes), it will be returned as is.
    """

    # Case 1 - Limit is a valid IEC unit
    if re.match(ibi_regex, limit):
        return limit

    # Case 2 - Limit is intended to be a SI unit, but k8s does not need the b
    if re.match(ilo_regex, limit):
        match = re.match(ilo_regex, limit)
        if not match:
            # This should NEVER happen, need to shut up the typer
            raise ValueError(f"Invalid limit size format: {limit}")
        size, unit = match.groups()

        return f"{size}{unit[0]}"

    # Limit is in bytes
    if re.match(dec_regex, limit):
        return f"{limit}"

    raise ValueError(f"Invalid limit size format: {limit}")
