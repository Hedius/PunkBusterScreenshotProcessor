from abc import ABC, abstractmethod
from typing import List


class Destination(ABC):
    """
    Common interface for screenshot upload destinations (e.g. FTP, SFTP).
    """
    public_base: str

    @abstractmethod
    def upload_screenshot(self, guid, data, client=None, full=False):
        """
        Upload a screenshot to the destination.
        Opens a connection if client is None. Returns the (possibly newly opened)
        connection so that it can be reused across calls.
        :param guid:
        :param data:
        :param client: optional open connection to reuse
        :param full: indicate that a full unedited screenshot is shared.
        :return: open connection
        """

    @abstractmethod
    def cleanup_screenshots(self, to_delete: List, client=None):
        """
        Remove the given files from the destination.
        :param to_delete: list containing screenshots
        :param client: optional open connection to reuse
        :return: open connection
        """
