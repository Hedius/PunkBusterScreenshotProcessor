import sys
import time
import uuid
from argparse import ArgumentParser
from configparser import ConfigParser
from pathlib import Path
from typing import List, Tuple

from loguru import logger

from adkats_db.adkats_db import AdKatsDB, DuplicateGuidError
from destination import Destination
from ftp.destination import DestinationFTP
from ftp.source import SourceFTP
from sftp.destination import DestinationSFTP

# random screenshot GUIDs can (extremely rarely) collide with an existing
# entry and violate the DB's unique index. Regenerate the GUID and retry.
MAX_INSERT_ATTEMPTS = 5


def esp_db_fields(esp):
    """
    Map an EspResult onto the four e4gl_screenshots columns.

    Returns all None when the screenshot was not scanned - which the DB keeps
    distinct from having been scanned and found clean.
    :param esp: EspResult or None
    :return: (esp_status, esp_score, esp_reason, esp_model)
    """
    if esp is None:
        return None, None, None, None
    if esp.capture_failed:
        # No score: nothing was rendered, so the frame is evidence of neither
        # cheating nor innocence.
        return 'capture_failed', None, None, esp.model_version
    status = 'flagged' if esp.flagged else 'clean'
    reason = ', '.join(esp.reasons)[:128] or None
    return status, round(esp.score, 2), reason, esp.model_version


def log_esp_result(esp, server_id, pb_guid, url):
    """
    Report the ESP verdict on stdout.

    Flagged screenshots go to WARNING with the URL so they stand out in the
    container log; clean ones sit at DEBUG because the insert already logs a
    line per screenshot.
    """
    if esp is None:
        return
    if esp.capture_failed:
        logger.info(f'ESP> capture failed (nothing rendered) - {url}')
    elif esp.flagged:
        logger.warning(f'ESP> FLAGGED score={esp.score:.1f}'
                       f' reasons={", ".join(esp.reasons)}'
                       f' server={server_id} player={pb_guid} {url}')
    else:
        logger.debug(f'ESP> clean score={esp.score:.1f} {url}')


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

                        for attempt in range(1, MAX_INSERT_ATTEMPTS + 1):
                            guid = uuid.uuid4()
                            base = self.destination.public_base.rstrip("/")
                            url = f'{base}/{guid}.{self.destination.file_extension}'
                            full_url = (f'{base}/full/'
                                       f'{guid}.{self.destination.file_extension}')

                            # cropped screenshot
                            client = self.destination.upload_screenshot(guid,
                                                                         data['data'],
                                                                         client=client)
                            # full sized screenshot
                            client = self.destination.upload_screenshot(guid,
                                                                         data['data_full'],
                                                                         client=client,
                                                                         full=True)
                            esp = data.get('esp')
                            status, score, reason, model = esp_db_fields(esp)
                            try:
                                self.adk.add_screenshot(source.server_id, data['pb_guid'],
                                                        data['timestamp'], guid, url,
                                                        full_url, status, score, reason,
                                                        model)
                                log_esp_result(esp, source.server_id, data['pb_guid'], url)
                                break
                            except DuplicateGuidError:
                                logger.warning(f'Screenshot GUID {guid} collided with an'
                                              f' existing entry, retrying'
                                              f' ({attempt}/{MAX_INSERT_ATTEMPTS})...')
                        else:
                            logger.critical('Failed to add screenshot to the DB after'
                                            f' {MAX_INSERT_ATTEMPTS} attempts due to'
                                            ' repeated GUID collisions.')

                to_delete = self.adk.cleanup_screenshots(self.cleanup_after)
                client = self.destination.cleanup_screenshots(to_delete, client)
            finally:
                if client:
                    client.quit()
            time.sleep(self.check_interval)


def read_esp_model(parser: ConfigParser):
    """
    Load the fitted ESP detection model once, for all sources to share.

    The model file is a few hundred KB, so it is read once at startup.
    :param parser: the parsed config
    :return: (EspModel or None, threshold override or None)
    """
    section = parser['ESPDetection'] if parser.has_section('ESPDetection') else None
    if section is None or not section.getboolean('enabled', True):
        logger.info('ESP detection is disabled.')
        return None, None

    threshold = section.getfloat('threshold', None)
    model_path = section.get('model_path') or None
    try:
        from image_processing import esp_detection
        model = esp_detection.EspModel.load(model_path or esp_detection.MODEL_PATH)
    except Exception as e:
        # A missing model or a missing OpenCV must not stop screenshot
        # collection - that is the job, detection is the bonus.
        logger.critical(f'ESP detection could not be enabled, screenshots will be'
                        f' stored unscanned: {e}')
        return None, None

    effective = threshold if threshold is not None else model.threshold
    logger.info(f'ESP detection enabled. Model {model.version}, threshold {effective:.2f}'
                + (' (overridden in config)' if threshold is not None else ''))
    return model, threshold


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
    compress = section.getboolean('compress', True)

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
            compress=compress,
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
            compress=compress,
        )
    else:
        raise RuntimeError(f'Unknown destination type: {dest_type}')

    esp_model, esp_threshold = read_esp_model(parser)

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
            compress=compress,
            esp_model=esp_model,
            esp_threshold=esp_threshold,
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
