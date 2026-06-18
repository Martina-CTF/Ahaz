# Mmmm... cider lives here now


def cidr_to_netmask(cidr: int) -> str:
    mask = (0xFFFFFFFF >> (32 - cidr)) << (32 - cidr)
    return f"{(mask >> 24) & 0xFF}.{(mask >> 16) & 0xFF}.{(mask >> 8) & 0xFF}.{mask & 0xFF}"


def ip_and_cidr_to_netmask(ip_cidr: str) -> str:
    ip, cidr = ip_cidr.split("/")
    cidr = int(cidr)
    netmask = cidr_to_netmask(cidr)
    return ip + " " + netmask


def parse_ip_range(ip_range: str) -> str:
    if ip_range.count("/") == 1:
        return ip_and_cidr_to_netmask(ip_range)
    elif ip_range.count(" ") == 1:
        return ip_range
    else:
        raise ValueError("Invalid IP range format")
