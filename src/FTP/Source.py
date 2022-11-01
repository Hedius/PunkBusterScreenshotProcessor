import logging
import re
from datetime import datetime
from io import BytesIO

from PIL import Image

from FTP.FTPBase import FTPBase


class SourceFTP(FTPBase):
    """
    Source FTP server handler.
    Downloads images, converts and crops them.
    """
    def __init__(self, server_id, host, port, user, pw, base_dir):
        """
        Source FTP server handler.
        Downloads images, converts and crops them.
        :param server_id:
        :param host:
        :param port:
        :param user:
        :param pw:
        :param base_dir:
        """
        super().__init__(host, port, user, pw, base_dir)
        self.server_id = server_id

    def fetch(self, min_timestamp):
        """
        Fetch all screenshots with a timestamp above min_timestamp
        :param min_timestamp:
        :return:
        """
        ftp = self.connect()
        try:
            data = self.read_text(ftp, 'pbsvss.htm')
            screenshots = get_screenshots_to_fetch(data, min_timestamp)
            self.fetch_screenshots(ftp, screenshots)
        finally:
            ftp.quit()
        return screenshots

    def fetch_screenshots(self, ftp, screenshots):
        """
        Downloads and converts the given screenshots to JPEG.
        :param ftp:
        :param screenshots:
        :return:
        """
        for screenshot_id in screenshots:
            file_name = f'pb{screenshot_id}.png'
            try:
                data = self.read_binary(ftp, file_name)
            except Exception as e:
                logging.critical(
                    f'Failed to fetch {file_name} from {self.server_id} - Ignoring: {e}')
                continue
            image = Image.open(BytesIO(data))
            side_crop = 190
            result = image.crop((side_crop, 0, image.width - side_crop, 220))

            processed_data = BytesIO()
            result.save(processed_data, format='JPEG')
            screenshots[screenshot_id]['data'] = processed_data.getvalue()
        return screenshots


def get_screenshots_to_fetch(data, min_timestamp):
    """
    Parses the punkbuster HTML file and extracts all screenshots with a timestamp
    above min_timestamp.
    :param data:
    :param min_timestamp:
    :return:
    """
    screenshots_to_fetch = {}
    for line in data:
        match = re.match(r'^.*blank>(.+)</a>\s+"(.+)".*GUID=(.+)\(-\)\s\[(.+)]$', line)
        if not match:
            continue
        screenshot_id = match.group(1)
        name = match.group(2)
        pb_guid = match.group(3)
        timestamp = datetime.strptime(match.group(4), '%Y.%m.%d %H:%M:%S')

        if timestamp <= min_timestamp:
            continue

        screenshots_to_fetch[screenshot_id] = {
            'id': screenshot_id,
            'name': name,
            'pb_guid': pb_guid,
            'timestamp': timestamp
        }
    return screenshots_to_fetch
