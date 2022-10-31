import mysql.connector as mariadb
from mysql.connector import errorcode


class Connector:
    """SQL connector for MySQL/MariaDB sql servers"""

    def __init__(self, host, port, database, user, pw):
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = pw

        self._con = None

    def __del__(self):
        """destructor - close connection"""
        self._close()

    def connect(self):
        """
        Connect to a MariaDB server and set/return the connection socket if
        needed.
        """
        # return active connection
        if self._con and self._con.is_connected():
            return self._con

        # reconnect
        try:
            self._con = mariadb.connect(host=self._host,
                                        port=self._port,
                                        database=self._database,
                                        user=self._user,
                                        password=self._password)
        except mariadb.Error as e:
            if e.errno == errorcode.ER_ACCESS_DENIED_ERROR:
                raise self.DBException(
                    f'Connection to database {self._database}'
                    ' failed! Invalid username/password!')
            elif e.errno == errorcode.ER_BAD_DB_ERROR:
                raise self.DBException(
                    f'Database {self._database} does not exist on host'
                    f' {self._host}:{self._port}')
            else:
                raise self.DBException(
                    f'Error while connecting to Database '
                    f'{self._database}\n{e}')
        return self._con

    def _close(self):
        """close the connection to the MariaDB server"""
        # return active connection
        if self._con and self._con.is_connected():
            self._con.close()

    def cursor(self):
        con = self.connect()
        return con.cursor(named_tuple=True)

    def exec(self, query, args=None):
        """
        Execute the given command with args
        :param query: sql query
        :param args: arguments
        :returns: result as a list
        """
        # support for multi queries?
        results = []
        con = self.connect()
        cursor = self.cursor()

        try:
            cursor.execute(query, args)
            for entry in cursor:
                results.append(entry)
            con.commit()
        except mariadb.Error as e:
            raise self.SQLException(f'Execution of query failed!: {e}')
        return results

    # exception
    class DBException(Exception):
        pass

    class SQLException(DBException):
        pass
