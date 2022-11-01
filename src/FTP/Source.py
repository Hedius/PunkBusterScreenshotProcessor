import logging
import re
from dateutil import parser
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
            data = ftp.mlsd()
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
        for file_name in screenshots:
            try:
                data = self.read_binary(ftp, file_name)
            except Exception as e:
                logging.critical(
                    f'Failed to fetch {file_name} from {self.server_id} - Ignoring: {e}')
                continue
            image = Image.open(BytesIO(data))
            comment = image.info['comment'].split('\n')
            match = re.match(r'^\*(.+)\*\s(.+)', comment[2])
            side_crop = 140
            result = image.crop((side_crop, 0, image.width - side_crop, 220))

            processed_data = BytesIO()
            result.save(processed_data, format='JPEG')
            screenshots[file_name]['pb_guid'] = match.group(1)
            screenshots[file_name]['name'] = match.group(2)
            screenshots[file_name]['data'] = processed_data.getvalue()
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
    for file in data:
        name = file[0]
        if not name.endswith('png'):
            continue
        timestamp = parser.parse(file[1]['modify'])
        if timestamp <= min_timestamp:
            continue
        screenshots_to_fetch[name] = {
            'timestamp': timestamp
        }
    return screenshots_to_fetch
