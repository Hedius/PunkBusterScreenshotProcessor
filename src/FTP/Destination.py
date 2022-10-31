import logging
from io import BytesIO

from src.FTP.FTPBase import FTPBase


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

    def upload_screenshot(self, guid, data, ftp=None):
        """
        Upload a screenshot to the destination FTP server.
        Opens a FTP socket if ftp is none. Returns the sockets.
        :param guid:
        :param data:
        :param ftp: optional ftp socket
        :return: open ftp socket
        """
        if not ftp:
            ftp = self.connect()
        ftp.storbinary(f'STOR {guid}.jpg', BytesIO(data))
        logging.debug(f'Saved screenshot for {guid} over FTP.')
        return ftp
