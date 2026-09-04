import paramiko


class SFTPConnection:
    """
    Wraps an open SFTP session together with its underlying SSH client so
    that it can be closed with a single call, similar to ftplib's
    FTP.quit().
    """
    def __init__(self, ssh_client: paramiko.SSHClient, sftp_client: paramiko.SFTPClient):
        self.ssh_client = ssh_client
        self.sftp_client = sftp_client

    def quit(self):
        """
        Close the SFTP session and the underlying SSH connection.
        """
        self.sftp_client.close()
        self.ssh_client.close()


class SFTPBase:
    """
    SFTP base
    """
    def __init__(self, host, port, user, host_key_path, pw=None, private_key_path=None,
                 private_key_passphrase=None, base_dir=None):
        """
        SFTP base
        :param host:
        :param port:
        :param user:
        :param host_key_path: path to a known_hosts style file used to verify the server's
                               host key.
        :param pw: password. Optional if a private key is used.
        :param private_key_path: path to a private key file. Optional if a password is used.
        :param private_key_passphrase: passphrase for the private key. Optional.
        :param base_dir:
        """
        self._host = host
        self._port = port
        self._user = user
        self._host_key_path = host_key_path
        self._pw = pw
        self._private_key_path = private_key_path
        self._private_key_passphrase = private_key_passphrase
        self._base_dir = base_dir

    def connect(self) -> SFTPConnection:
        """
        Open a connection to the configured SFTP server.
        Verifies the server's host key against the configured known_hosts file and
        rejects the connection if it does not match.
        Changes directory to the given base dir if configured.
        :return: SFTPConnection
        """
        timeout = 30

        ssh_client = paramiko.SSHClient()
        ssh_client.load_host_keys(str(self._host_key_path))
        ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())

        key_filename = str(self._private_key_path) if self._private_key_path else None
        ssh_client.connect(
            hostname=self._host,
            port=self._port,
            username=self._user,
            password=self._pw,
            key_filename=key_filename,
            passphrase=self._private_key_passphrase,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )

        sftp_client = ssh_client.open_sftp()
        if self._base_dir:
            sftp_client.chdir(self._base_dir)

        return SFTPConnection(ssh_client, sftp_client)
