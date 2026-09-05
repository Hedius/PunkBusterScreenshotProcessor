import re
from dateutil import parser
from io import BytesIO

from PIL import Image
from loguru import logger

from ftp.ftp_base import FTPBase
from image_processing import crop

try:
    from image_processing import esp_detection
except ImportError:  # numpy / OpenCV absent - ESP detection stays optional
    esp_detection = None


class SourceFTP(FTPBase):
    """
    Source FTP server handler.
    Downloads images, converts and crops them.
    """
    def __init__(self, server_id, host, port, user, pw, base_dir, compress=True,
                 esp_model=None, esp_threshold=None):
        """
        Source FTP server handler.
        Downloads images, converts and crops them.
        :param server_id:
        :param host:
        :param port:
        :param user:
        :param pw:
        :param base_dir:
        :param compress: convert the screenshots to JPEG. Keeps PNG if disabled.
        :param esp_model: loaded EspModel. Screenshots are scanned for cheat ESP
            overlays when given, and left unscanned when None.
        :param esp_threshold: override the threshold fitted into the model.
        """
        super().__init__(host, port, user, pw, base_dir)
        self.server_id = server_id
        self.compress = compress
        self.esp_model = esp_model
        self.esp_threshold = esp_threshold

    @property
    def image_format(self):
        """
        PIL format the screenshots are saved with.
        :return: JPEG for compressed screenshots, otherwise PNG
        """
        return 'JPEG' if self.compress else 'PNG'

    def fetch(self, min_timestamp):
        """
        Fetch all screenshots with a timestamp above min_timestamp
        :param min_timestamp:
        :return:
        """
        ftp = self.connect()
        try:
            data = ftp.mlsd()
            screenshots = get_screenshots_to_fetch(data, min_timestamp)
            self.fetch_screenshots(ftp, screenshots)
        finally:
            ftp.quit()
        return screenshots

    def fetch_screenshots(self, ftp, screenshots):
        """
        Downloads and converts the given screenshots to JPEG or PNG.
        :param ftp:
        :param screenshots:
        :return:
        """
        for file_name in screenshots:
            try:
                data = self.read_binary(ftp, file_name)
            except Exception as e:
                logger.critical(
                    f'Failed to fetch {file_name} from {self.server_id} - Ignoring: {e}')
                continue
            image = Image.open(BytesIO(data))
            full_data = BytesIO()
            image.save(full_data, format=self.image_format)

            comment = image.info['comment']
            lines = comment.split('\n')
            match = re.match(r'^\*(.+)\*', lines[2])

            result = crop.crop(image, comment)
            # Scan before saving: the detector wants the cropped frame as pixels,
            # not the compressed bytes, because JPEG artifacts cost recall.
            screenshots[file_name]['esp'] = self.detect_esp(result)

            processed_data = BytesIO()
            result.save(processed_data, format=self.image_format)

            screenshots[file_name]['pb_guid'] = match.group(1)
            screenshots[file_name]['data'] = processed_data.getvalue()
            screenshots[file_name]['data_full'] = full_data.getvalue()
        return screenshots

    def detect_esp(self, image):
        """
        Score a cropped screenshot for cheat ESP overlays.

        Never raises: screenshot collection is the job here, and an unscanned
        screenshot is far better than a crashed poll loop.
        :param image: cropped PIL image, uncompressed
        :return: EspResult, or None when detection is disabled or failed
        """
        if self.esp_model is None:
            return None
        try:
            return esp_detection.detect(image, model=self.esp_model,
                                        threshold=self.esp_threshold, explain=False)
        except Exception as e:
            logger.critical(f'ESP detection failed on a screenshot from server'
                            f' {self.server_id} - storing it unscanned: {e}')
            return None


def get_screenshots_to_fetch(data, min_timestamp):
    """
    Parses the punkbuster HTML file and extracts all screenshots with a timestamp
    above min_timestamp.
    :param data:
    :param min_timestamp:
    :return:
    """
    screenshots_to_fetch = {}
    for file in data:
        name = file[0]
        if not name.endswith('png'):
            continue
        timestamp = parser.parse(file[1]['modify'])
        if timestamp <= min_timestamp:
            continue
        screenshots_to_fetch[name] = {
            'timestamp': timestamp
        }
    return screenshots_to_fetch
