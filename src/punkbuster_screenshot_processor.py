import sys
import time
import uuid
from argparse import ArgumentParser
from configparser import ConfigParser
from pathlib import Path
from typing import List, Tuple

from loguru import logger

from adkats_db.adkats_db import AdKatsDB
from destination import Destination
from ftp.destination import DestinationFTP
from ftp.source import SourceFTP
from sftp.destination import DestinationSFTP


class Processor:
    def __init__(self, adk: AdKatsDB, sources: List[SourceFTP], destination: Destination,
                 check_interval=60, cleanup_after=60):
        """
        Init Processor
        :param adk:
        :param sources:
        :param destination:
        :param check_interval: repeat checks every x seconds
        :param cleanup_after: cleanup screenshots after X days
        """
        self.adk = adk
        self.sources = sources
        self.destination = destination
        self.check_interval = check_interval

        self.cleanup_after = cleanup_after

    def run(self):
        """
        Endless loop

        1. go through all servers
        2. Fetch the max timestamp
        3. Fetch screenshots above the timestamp
        4. crop them convert them to JPG
        5. upload them to the destination FTP server (apache in our case)
        6. insert an entry into the procon DB.
        """
        while True:
            # connection for caching
            client = None
            try:
                for source in self.sources:
                    try:
                        last_timestamp = self.adk.get_latest_timestamp(source.server_id)
                        screenshots = source.fetch(last_timestamp)
                    except Exception as e:
                        logger.critical(f'Failed to fetch server {source.server_id}!'
                                        f' Skipping it. - Error: {e}')
                        continue

                    for screenshot_id in screenshots:
                        data = screenshots[screenshot_id]
                        if 'data' not in data:
                            continue
                        guid = uuid.uuid4()
                        url = f'{self.destination.public_base.rstrip("/")}/{guid}.jpg'

                        # cropped screenshot
                        client = self.destination.upload_screenshot(guid,
                                                                     data['data'],
                                                                     client=client)
                        # full sized screenshot
                        client = self.destination.upload_screenshot(guid,
                                                                     data['data_full'],
                                                                     client=client,
                                                                     full=True)
                        self.adk.add_screenshot(source.server_id, data['pb_guid'],
                                                data['timestamp'], url)

                to_delete = self.adk.cleanup_screenshots(self.cleanup_after)
                client = self.destination.cleanup_screenshots(to_delete, client)
            finally:
                if client:
                    client.quit()
            time.sleep(self.check_interval)


def read_config(file_path: Path) -> Tuple[int, int, AdKatsDB, List[SourceFTP], Destination]:
    """
    Read the config
    :param file_path:
    :return: logging_interval, adk, influx
    """
    parser = ConfigParser()
    parser.read(file_path)

    section = parser['General']
    check_interval = section.getint('check_interval', 60)
    cleanup_after = section.getint('cleanup_after_days', 60)
    timezone = section.get('timezone', 'Europe/Berlin')

    section = parser['AdKatsDB']
    adk = AdKatsDB(
        host=section['host'],
        port=int(section['port']),
        user=section['user'],
        pw=section['pw'],
        database=section['database'],
        timezone=timezone,
    )

    section = parser['Destination']
    dest_type = section.get('type', 'ftp').strip().lower()
    if dest_type == 'ftp':
        destination = DestinationFTP(
            host=section['host'],
            port=int(section['port']),
            user=section['user'],
            pw=section['pw'],
            public_base=section['public_base'],
            upload_dir=section['upload_dir'],
            tls=section.getboolean('tls'),
        )
    elif dest_type == 'sftp':
        destination = DestinationSFTP(
            host=section['host'],
            port=int(section['port']),
            user=section['user'],
            pw=section.get('pw') or None,
            private_key_path=section.get('private_key_path') or None,
            private_key_passphrase=section.get('private_key_passphrase') or None,
            host_key_path=section['host_key_path'],
            public_base=section['public_base'],
            upload_dir=section['upload_dir'],
        )
    else:
        raise RuntimeError(f'Unknown destination type: {dest_type}')

    sources = []
    for title in parser.sections():
        if not title.lower().startswith('server'):
            continue
        section = parser[title]
        source = SourceFTP(
            server_id=int(title.lower().lstrip('server')),
            host=section['host'],
            port=int(section['port']),
            user=section['user'],
            pw=section['pw'],
            base_dir=section['base_dir'],
        )
        sources.append(source)

    if len(sources) == 0:
        raise RuntimeError('At least one source is needed!')

    return check_interval, cleanup_after, adk, sources, destination


def main():
    logger.remove()
    logger.add(sys.stderr, level='INFO')
    parser = ArgumentParser(description='E4GL Screenshot Processor')
    parser.add_argument(
        '-c', '--config',
        help='Path to config file',
        required=True,
        dest='config'
    )
    args = parser.parse_args()

    (check_interval, cleanup_after, adk, sources, destination) = \
        read_config(args.config)

    worker = Processor(adk, sources, destination, check_interval,
                       cleanup_after)
    worker.run()


if __name__ == '__main__':
    main()
