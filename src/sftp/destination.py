import logging
import re
from io import BytesIO
from typing import List, Optional

from sftp.sftp_base import SFTPBase, SFTPConnection


class DestinationSFTP(SFTPBase):
    """
    SFTP Upload logic
    """
    def __init__(self, host, port, user, host_key_path, public_base, upload_dir, pw=None,
                 private_key_path=None, private_key_passphrase=None):
        """
        SFTP Uploader to destination
        :param host:
        :param port:
        :param user:
        :param host_key_path: path to a known_hosts style file used to verify the server's
                               host key.
        :param public_base: http base / URL
        :param upload_dir:
        :param pw: password. Optional if a private key is used.
        :param private_key_path: path to a private key file. Optional if a password is used.
        :param private_key_passphrase: passphrase for the private key. Optional.
        """
        super().__init__(host, port, user, host_key_path, pw=pw,
                          private_key_path=private_key_path,
                          private_key_passphrase=private_key_passphrase, base_dir=upload_dir)
        self.public_base = public_base

    def upload_screenshot(self, guid, data, sftp: Optional[SFTPConnection] = None, full=False):
        """
        Upload a screenshot to the destination SFTP server.
        Opens a SFTP connection if sftp is none. Returns the connection.
        :param guid:
        :param data:
        :param sftp: optional SFTPConnection
        :param full: indicate that a full unedited screenshot is shared.
        :return: open SFTPConnection
        """
        full_dir = 'full'
        if not sftp:
            sftp = self.connect()
            try:
                sftp.sftp_client.mkdir(full_dir)
            except OSError:
                pass
        directory = f'{full_dir}/' if full else ''
        sftp.sftp_client.putfo(BytesIO(data), f'{directory}{guid}.jpg')
        logging.debug(f'Saved screenshot for {guid} over SFTP.')
        return sftp

    def cleanup_screenshots(self, to_delete: List, sftp: Optional[SFTPConnection] = None):
        """
        Remove the given files from the disk over SFTP.
        :param to_delete: list containing screenshots
        :param sftp: optional SFTPConnection
        :return: open SFTPConnection
        """
        if not sftp:
            sftp = self.connect()
        for screenshot in to_delete:
            match = re.match(r'.*/([^/]+)$', screenshot.url)
            file = match.group(1)

            try:
                sftp.sftp_client.remove(file)
                sftp.sftp_client.remove(f'full/{file}')
                logging.info(f'Cleanup> Removed {screenshot.url} from the file system.')
            except OSError as e:
                logging.info(f'Cleanup> Failed to remove screenshot from file system: {e}.')

        return sftp
