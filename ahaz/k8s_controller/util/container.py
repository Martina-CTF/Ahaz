from ahaz_common.task import ImageInformation


def get_image_name(image: ImageInformation) -> str:
    """
    Returns the full, canonical name of the image based on the information in the ImageInformation object.
    """
    if image.registry:
        return f"{image.registry}/{image.name}"
    return image.name

