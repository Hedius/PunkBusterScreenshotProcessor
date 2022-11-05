import logging
from io import BytesIO

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
            except:
                pass
        directory = f'{full_dir}/' if full else ''
        ftp.storbinary(f'STOR {directory}{guid}.jpg', BytesIO(data))
        logging.debug(f'Saved screenshot for {guid} over FTP.')
        return ftp
