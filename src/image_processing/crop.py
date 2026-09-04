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

    # Should be the left border of the image.
    offset_x = round((image.width - resulting_w) / 2)
    # 10px from top
    top_border = 10

    box = (
        max(0, offset_x),
        top_border,
        min(image.width, offset_x + resulting_w),
        min(image.height, top_border + resulting_h),
    )
    return image.crop(box)
