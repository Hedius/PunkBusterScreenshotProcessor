import logging
import re
from ftplib import error_perm
from io import BytesIO
from typing import List

from FTP.FTPBase import FTPBase


class DestinationFTP(FTPBase):
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

    def upload_screenshot(self, guid, data, ftp=None, full=False):
        """
        Upload a screenshot to the destination FTP server.
        Opens a FTP socket if ftp is none. Returns the sockets.
        :param guid:
        :param data:
        :param ftp: optional ftp socket
        :param full: indicate that a full unedited screenshot is shared.
        :return: open ftp socket
        """
        full_dir = 'full'
        if not ftp:
            ftp = self.connect()
            try:
                ftp.mkd(full_dir)
            except error_perm:
                pass
        directory = f'{full_dir}/' if full else ''
        ftp.storbinary(f'STOR {directory}{guid}.jpg', BytesIO(data))
        logging.debug(f'Saved screenshot for {guid} over FTP.')
        return ftp

    def cleanup_screenshots(self, to_delete: List, ftp=None):
        """
        Remove the given files from the disk over FTP.
        :param to_delete: list containing screenshots
        :param ftp: optional ftp socket
        :return: open ftp socket
        """
        if not ftp:
            ftp = self.connect()
        for screenshot in to_delete:
            match = re.match(r'.*/([^/]+)$', screenshot.url)
            file = match.group(1)

            try:
                ftp.delete(file)
                ftp.delete(f'full/{file}')
                logging.info(f'Cleanup> Removed {screenshot.url} from the file system.')
            except error_perm as e:
                logging.info(f'Cleanup> Failed to remove screenshot from file system: {e}.')

        return ftp
