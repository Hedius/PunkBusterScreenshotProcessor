from datetime import datetime

import mariadb
from loguru import logger
from mariadb.constants import ERR

from .db.connector import Connector


class DuplicateGuidError(Exception):
    """Raised when a screenshot's random GUID collides with an existing entry."""


# Columns added along with ESP detection. See the README for the ALTER statement.
ESP_COLUMNS = ('esp_status', 'esp_score', 'esp_reason', 'esp_model')


class AdKatsDB(Connector):
    def __init__(self, host, port, user, pw, database, timezone='Europe/Berlin'):
        super().__init__(host, port, database, user, pw)
        self.timezone = timezone
        self.verify_schema()
        self.update_functions()

    def verify_schema(self):
        """
        Fail fast when the ESP columns are missing.

        Without this the service looks healthy but every insert fails on an
        unknown column, while uploads keep succeeding - so screenshots pile up
        on the webserver with no DB row pointing at them and nothing to clean
        them up. Better to refuse to start and say exactly what to run.
        :raises RuntimeError: if e4gl_screenshots is missing the esp_* columns
        """
        command = """\
        SELECT COLUMN_NAME AS name
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'e4gl_screenshots'
        """
        present = {row.name.lower() for row in self.exec(command)}
        if not present:
            raise RuntimeError('Table e4gl_screenshots does not exist. See the README.')
        missing = [column for column in ESP_COLUMNS if column not in present]
        if missing:
            raise RuntimeError(
                f'Table e4gl_screenshots is missing the column(s) {", ".join(missing)}.'
                ' Apply the migration from the README before starting this version:\n'
                '    alter table e4gl_screenshots\n'
                "        add column esp_status enum ('clean', 'flagged',"
                " 'capture_failed') null,\n"
                '        add column esp_score  decimal(5, 2) null,\n'
                '        add column esp_reason varchar(128)  null,\n'
                '        add column esp_model  varchar(32)   null;')

    def update_functions(self):
        """
        Update the function for inserting new screenshots
        :return:
        """
        command = """\
        -- Not ideal... IN INOUT OUT are not support by mariaDB 10.5 :((((
        CREATE OR REPLACE FUNCTION add_screenshot(server_id SMALLINT UNSIGNED,
                                                  pb_guid TEXT,
                                                  pb_timestamp DATETIME,
                                                  screenshot_guid CHAR(36),
                                                  public_url TEXT,
                                                  public_full_url TEXT,
                                                  p_esp_status TEXT,
                                                  p_esp_score DECIMAL(5,2),
                                                  p_esp_reason TEXT,
                                                  p_esp_model TEXT)
                                                  RETURNS TEXT
        BEGIN
            SET @error_text = 'Successful';

            SELECT PlayerID INTO @player_id FROM tbl_playerdata pd WHERE pd.PBGUID = pb_guid
                LIMIT 1;
            IF @player_id IS NULL THEN
                SET @error_text = 'Failed to insert screenshot. Unknown Punkbuster ID';
                RETURN @error_text;
            END IF;

            -- get server_id
            SELECT ServerID INTO @server_id FROM tbl_server s WHERE s.ServerID = server_id
                LIMIT 1;
            IF @server_id IS NULL THEN
                SET @error_text = 'Invalid server ID given';
                RETURN @error_text;
            END IF;

            INSERT INTO e4gl_screenshots(server_id, player_id, timestamp, guid, url,
                                         full_url, esp_status, esp_score, esp_reason,
                                         esp_model)
            VALUES(@server_id, @player_id, pb_timestamp, screenshot_guid, public_url,
                   public_full_url, p_esp_status, p_esp_score, p_esp_reason, p_esp_model);
            RETURN @error_text;
        END;
        """
        self.exec(command)

    def get_latest_timestamp(self, server_id: int):
        """
        Get the latest timestamp for a given server.
        :param server_id: players
        """
        command = """\
              SELECT
                  CONVERT_TZ(MAX(timestamp), %s, 'GMT') AS timestamp
              FROM e4gl_screenshots
              WHERE server_id = %s
              GROUP BY server_id
              """
        result = self.exec(command, (self.timezone, server_id))
        if len(result) == 0:
            return datetime.now().replace(year=2000)
        timestamp = result[0][0]
        return timestamp

    def cleanup_screenshots(self, cleanup_after: int):
        """
        Get outdated screenshots which can be deleted.
        Also remove them from the DB.

        Screenshots of banned players are never deleted, and neither are
        screenshots flagged for a suspected ESP overlay - those are the ones an
        admin still needs to look at, so expiring them silently would throw away
        exactly the evidence the detection exists to surface.
        :param cleanup_after:
        """
        command = """\
        SELECT screenshot_id, url
        FROM e4gl_screenshots es
        LEFT OUTER JOIN adkats_bans ab on es.player_id = ab.player_id
        WHERE DATE_SUB(NOW(), INTERVAL %s DAY) >= timestamp
        AND (ab.ban_status != 'Active' or ban_status is null)
        AND (es.esp_status IS NULL OR es.esp_status != 'flagged')
        """
        result = self.exec(command, (cleanup_after,))
        command = """\
        DELETE FROM e4gl_screenshots
        WHERE screenshot_id = %s
        """
        for screenshot in result:
            self.exec(command, (screenshot.screenshot_id,))
            logger.info(f'Cleanup> Removed {screenshot.url} from the DB.')
        return result

    def add_screenshot(self, server_id, pb_guid, timestamp, guid, url, full_url,
                       esp_status=None, esp_score=None, esp_reason=None, esp_model=None):
        """
        Insert the given screenshot to the procon database.
        :param server_id:
        :param pb_guid: PunkBuster GUID of the player, used to resolve the player_id.
        :param timestamp:
        :param guid: random UUID identifying this screenshot. Unique in the DB.
        :param url:
        :param full_url:
        :param esp_status: 'clean', 'flagged' or 'capture_failed'. None when the
            screenshot was not scanned, which is distinct from being clean.
        :param esp_score: 0-100 suspicion score. None for unscanned screenshots and
            for failed captures, where a score would be meaningless.
        :param esp_reason: comma separated names of the signals that fired.
        :param esp_model: content hash of the model that produced the score.
        :raises DuplicateGuidError: if guid already exists in the DB. Callers should
            retry with a freshly generated guid.
        """
        commands = """\
        SELECT add_screenshot(%s, %s, Convert_TZ(%s, 'GMT', %s), %s, %s, %s,
                              %s, %s, %s, %s) AS result;
        """
        args = (server_id, pb_guid, timestamp, self.timezone, str(guid), url, full_url,
                esp_status, esp_score, esp_reason, esp_model)
        con = self.connect()
        cursor = self.cursor()

        try:
            cursor.execute(commands, args)
            result = cursor.fetchone().result
            if result == 'Successful':
                con.commit()
                logger.info(f'Added a new screenshot to the DB: {url}')
            else:
                con.rollback()
                logger.critical(f'Failed to add'
                                f' new screenshot to the DB: {result}')
        except mariadb.Error as e:
            con.rollback()
            if e.errno == ERR.ER_DUP_ENTRY:
                raise DuplicateGuidError(
                    f'Screenshot GUID {guid} already exists in the DB') from e
            logger.critical(f'Failed to add new screenshot to the DB for guid'
                            f' {pb_guid}: {e}')
        finally:
            cursor.close()
            con.close()
