import logging
import time
import uuid
from argparse import ArgumentParser
from configparser import ConfigParser
from pathlib import Path
from typing import Tuple, List

from AdKatsDB.AdKatsDB import AdKatsDB
from FTP.Source import SourceFTP
from FTP.Destination import DestinationFTP


class Processor:
    def __init__(self, adk: AdKatsDB, sources: List[SourceFTP], destination: DestinationFTP,
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
            # socket for caching
            ftp_dest = None
            try:
                for source in self.sources:
                    last_timestamp = self.adk.get_latest_timestamp(source.server_id)
                    screenshots = source.fetch(last_timestamp)

                    for screenshot_id in screenshots:
                        data = screenshots[screenshot_id]
                        if 'data' not in data:
                            continue
                        guid = uuid.uuid4()
                        url = f'{self.destination.public_base.rstrip("/")}/{guid}.jpg'

                        # cropped screenshot
                        ftp_dest = self.destination.upload_screenshot(guid,
                                                                      data['data'],
                                                                      ftp=ftp_dest)
                        # full sized screenshot
                        ftp_dest = self.destination.upload_screenshot(guid,
                                                                      data['data_full'],
                                                                      ftp=ftp_dest,
                                                                      full=True)
                        self.adk.add_screenshot(source.server_id, data['pb_guid'],
                                                data['timestamp'], url)

                to_delete = self.adk.cleanup_screenshots(self.cleanup_after)
                ftp_dest = self.destination.cleanup_screenshots(to_delete, ftp_dest)
            finally:
                if ftp_dest:
                    ftp_dest.quit()
            time.sleep(self.check_interval)


def read_config(file_path: Path) -> Tuple[int, int, AdKatsDB,
                                          List[SourceFTP], DestinationFTP]:
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

    section = parser['AdKatsDB']
    adk = AdKatsDB(
        host=section['host'],
        port=int(section['port']),
        user=section['user'],
        pw=section['pw'],
        database=section['database'],
    )

    section = parser['Destination']
    destination = DestinationFTP(
        host=section['host'],
        port=int(section['port']),
        user=section['user'],
        pw=section['pw'],
        public_base=section['public_base'],
        upload_dir=section['upload_dir'],
        tls=section.getboolean('tls'),
    )

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
    logging.basicConfig(level=logging.INFO)
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
