import re

from loguru import logger


def crop(image, comment):
    """
    Crop away PunkBuster's burned-in info text and any letterbox padding.

    the size info is in the comment of the screenshot.
    Crop to it.

    :param image: the PIL image
    :param comment: the raw PNG comment string
    :return: cropped PIL image
    """
    resulting = re.search(r'Resulting:\s*w=(\d+)\s*X\s*h=(\d+)', comment)
    if not resulting:
        logger.warning('Could not find scaling info in screenshot comment - '
                        'falling back to the static crop.')
        side_crop = 140
        return image.crop((side_crop, 0, image.width - side_crop, 220))

    resulting_w, resulting_h = (int(g) for g in resulting.groups())

    # The real content is horizontally centered inside the canvas, which is
    # often wider than the picture because the info text line below it is
    # longer than the screenshot, but it is always flush against the top -
    # the "Attempted"/anchor fields turn out to describe the source-image
    # scaling, not the placement of the result, and are not needed here.
    offset_x = round((image.width - resulting_w) / 2)

    box = (
        max(0, offset_x),
        0,
        min(image.width, offset_x + resulting_w),
        min(image.height, resulting_h),
    )
    return image.crop(box)
