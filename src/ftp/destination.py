import re
from ftplib import error_perm
from io import BytesIO
from typing import List

from loguru import logger

from destination import Destination
from ftp.ftp_base import FTPBase


class DestinationFTP(FTPBase, Destination):
    """
    FTP Upload logic
    """
    def __init__(self, host, port, user, pw, public_base, upload_dir, tls):
        """
        FTP Uploader to destination
        :param host:
        :param port:
        :param user:
        :param pw:
        :param public_base: http base / URL
        :param upload_dir:
        :param tls:
        """
        super().__init__(host, port, user, pw, upload_dir, tls=tls)
        self.public_base = public_base

    def upload_screenshot(self, guid, data, client=None, full=False):
        """
        Upload a screenshot to the destination FTP server.
        Opens a FTP connection if client is none. Returns the connection.
        :param guid:
        :param data:
        :param client: optional ftp connection
        :param full: indicate that a full unedited screenshot is shared.
        :return: open ftp connection
        """
        full_dir = 'full'
        if not client:
            client = self.connect()
            try:
                client.mkd(full_dir)
            except error_perm:
                pass
        directory = f'{full_dir}/' if full else ''
        client.storbinary(f'STOR {directory}{guid}.jpg', BytesIO(data))
        logger.debug(f'Saved screenshot for {guid} over FTP.')
        return client

    def cleanup_screenshots(self, to_delete: List, client=None):
        """
        Remove the given files from the disk over FTP.
        :param to_delete: list containing screenshots
        :param client: optional ftp connection
        :return: open ftp connection
        """
        if not client:
            client = self.connect()
        for screenshot in to_delete:
            match = re.match(r'.*/([^/]+)$', screenshot.url)
            file = match.group(1)

            try:
                client.delete(file)
                client.delete(f'full/{file}')
                logger.info(f'Cleanup> Removed {screenshot.url} from the file system.')
            except error_perm as e:
                logger.info(f'Cleanup> Failed to remove screenshot from file system: {e}.')

        return client
