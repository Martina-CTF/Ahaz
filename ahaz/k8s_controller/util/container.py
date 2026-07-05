import re

from ahaz_common.task import ImageInformation


def get_image_name(image: ImageInformation) -> str:
    if image.registry:
        return f"{image.registry}/{image.name}"
    return image.name


size_regex = r"^(\d+)([KMGTP]b?)?$"


def adapt_limit_size(limit: str) -> str:
    """
    Adapt the limit size to a format that Kubernetes can understand.
    For example, if the limit is "512Mb", it will be converted to "512Mi".
    """
    match = re.match(size_regex, limit)

    if not match:
        if limit[:-1] == "i":
            return limit  # it's already *ibi
        raise ValueError(f"Invalid limit size format: {limit}")

    size, unit = match.groups()

    # Conver *ilo to *ibi
    new_size = int(size) * 1000 // 1024  # floor that shi

    return f"{new_size}{unit[0]}i" if unit else f"{new_size}Mi"  # assume Mi as default
