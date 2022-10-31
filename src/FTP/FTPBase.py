from ftplib import FTP, FTP_TLS
from typing import List


class FTPBase:
    """
    FTP base
    """
    def __init__(self, host, port, user, pw, base_dir=None, tls=False):
        """
        FTP base
        :param host:
        :param port:
        :param user:
        :param pw:
        :param base_dir:
        :param tls:
        """
        self._host = host
        self._port = port
        self._user = user
        self._pw = pw
        self._base_dir = base_dir
        self._tls = tls

    def connect(self) -> FTP:
        """
        Open a connection to the configured FTP server.
        Changes directory to the given base dir if configured.
        :return: ftp socket
        """
        ftp = FTP_TLS() if self._tls else FTP()
        ftp.connect(self._host, self._port)
        ftp.login(self._user, self._pw)
        if self._base_dir:
            ftp.cwd(self._base_dir)
        # setup a encrypted data channel
        if self._tls:
            ftp.prot_p()
        return ftp

    @staticmethod
    def read_binary(ftp: FTP, name):
        """
        Read the given binary file.
        :param ftp: socket
        :param name:
        :return: binary data
        """
        def handle_binary(more_data):
            data.append(more_data)
        data = []
        ftp.retrbinary(f'RETR {name}', callback=handle_binary)
        return b''.join(data)

    @staticmethod
    def read_text(ftp: FTP, name) -> List[str]:
        """
        Reads the given text file.
        :param ftp: socket
        :param name:
        :return: list containing each line of the text file.
        """
        def handle_text(more_data):
            data.append(more_data)
        data = []
        ftp.retrlines(f'RETR {name}', callback=handle_text)
        return data
