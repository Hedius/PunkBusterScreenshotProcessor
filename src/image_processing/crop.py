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
    attempted = re.search(
        r'Attempted:\s*w=(\d+)\s*X\s*h=(\d+)\s*at\s*\(x=(\d+)%,\s*y=(\d+)%\)', comment)
    resulting = re.search(r'Resulting:\s*w=(\d+)\s*X\s*h=(\d+)', comment)
    if not attempted or not resulting:
        logger.warning('Could not find scaling info in screenshot comment - '
                        'falling back to the static crop.')
        side_crop = 140
        return image.crop((side_crop, 0, image.width - side_crop, 220))

    attempted_w, attempted_h, anchor_x, anchor_y = (int(g) for g in attempted.groups())
    resulting_w, resulting_h = (int(g) for g in resulting.groups())

    offset_x = round((attempted_w - resulting_w) * (anchor_x / 100))
    offset_y = round((attempted_h - resulting_h) * (anchor_y / 100))

    box = (
        max(0, offset_x),
        max(0, offset_y),
        min(image.width, offset_x + resulting_w),
        min(image.height, offset_y + resulting_h),
    )
    return image.crop(box)
