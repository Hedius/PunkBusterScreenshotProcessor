import logging
from datetime import datetime

from .MariaDB.Connector import Connector


class AdKatsDB(Connector):
    def __init__(self, host, port, user, pw, database):
        super().__init__(host, port, database, user, pw)
        self.update_functions()

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
                                                  public_url TEXT)
                                                  RETURNS TEXT
        BEGIN
            SET @error_text = 'Successful';

            -- get player_id
            SELECT PlayerID INTO @player_id FROM tbl_playerdata pd WHERE pd.PBGUID = pb_guid;
            IF @player_id IS NULL THEN
                SET @error_text = 'Failed to insert screenshot. Unknown Punkbuster ID';
                RETURN @error_text;
            END IF;

            -- get server_id
            SELECT ServerID INTO @server_id FROM tbl_server s WHERE s.ServerID = server_id;
            IF @server_id IS NULL THEN
                SET @error_text = 'Invalid server ID given';
                RETURN @error_text;
            END IF;

            INSERT INTO e4gl_screenshots(server_id, player_id, timestamp, url)
            VALUES(@server_id, @player_id, pb_timestamp, public_url);
            RETURN @error_text;
        END;
        """
        self.exec(command)

    def get_latest_timestamp(self, server_id: int):
        """
        Get the latest timestamp for a given server.
        :param server_id: players
        """
        # ToDo The timezone is hardcoded here... fine for me... i live here :)
        # But for non EU users bad...
        command = """\
              SELECT
                  CONVERT_TZ(MAX(timestamp), 'Europe/Berlin', 'GMT') AS timestamp
              FROM e4gl_screenshots
              WHERE server_id = %s
              GROUP BY server_id
              """
        result = self.exec(command, (server_id,))
        if len(result) == 0:
            return datetime.now().replace(year=2000)
        timestamp = result[0][0]
        return timestamp

    def add_screenshot(self, server_id, pb_guid, timestamp, url):
        """
        Insert the given screenshot to the procon database.
        :param server_id:
        :param pb_guid:
        :param timestamp:
        :param url:
        :return:
        """
        commands = """\
        SELECT add_screenshot(%s, %s, Convert_TZ(%s, 'GMT', 'Europe/Berlin'), %s) AS result;
        """
        args = (server_id, pb_guid, timestamp, url)
        con = self.connect()
        cursor = self.cursor()

        cursor.execute(commands, args)
        result = cursor.fetchone().result
        if result == 'Successful':
            con.commit()
            logging.info(f'Added a new screenshot to the DB: {url}')
        else:
            con.rollback()
            logging.critical(f'Failed to add'
                             f' new screenshot to the DB: {result}')

        cursor.close()
        con.close()
