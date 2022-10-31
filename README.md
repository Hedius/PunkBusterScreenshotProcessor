# PunkBuster Screenshot Processor

[![pipeline status](https://gitlab.com/e4gl/punkbusterscreenshotprocessor/badges/main/pipeline.svg)](https://gitlab.com/e4gl/punkbusterscreenshotprocessor/-/commits/main)
[![Discord](https://img.shields.io/discord/388757799875903489.svg?colorB=7289DA&label=Discord&logo=Discord&logoColor=7289DA&style=flat-square)](https://discord.e4gl.com/)
<!-- [![Docker Pulls](https://img.shields.io/docker/pulls/hedius/bf4metricsloggger.svg?style=flat-square)](https://hub.docker.com/r/hedius/bf4metricslogger/) -->

# What does this do
1. Pull punkbuster screenshots from several servers over FTP.
2. Convert them to JPEG and crop them to remove the integrated text information.
3. Upload them to a destination FTP/FTPs server with a random guid.
4. Store the date in a table in the PRoCon DB.

This can be used for viewing the screenshots of your players.
You can then integrate this into your BFACP or to your discord like we did.

If you have any questions pls contact Hedius#0001 on Discord.

# Setup
## 1. Create the required table
```sql
create table e4gl_screenshots
(
    screenshot_id int UNSIGNED         auto_increment,
    server_id     smallint(5) UNSIGNED not null,
    player_id     int(10) UNSIGNED     not null,
    timestamp     datetime             not null,
    url           VARCHAR(128)         not null,
    constraint e4gl_screenshots_pk
        primary key (screenshot_id),
    constraint e4gl_screenshots_tbl_playerdata_fk
        foreign key (player_id) references tbl_playerdata (PlayerID)
            on delete cascade,
    constraint e4gl_screenshots_tbl_server_fk
        foreign key (server_id) references tbl_server (ServerID)
            on delete cascade
)
    comment 'Storage for PunkBuster Screenshot URLs';

create index e4gl_screenshots_player_id_index
    on e4gl_screenshots (player_id);

create index e4gl_screenshots_player_id_timestamp_index
    on e4gl_screenshots (player_id, timestamp);

```

## 2. docker (docker-compose)
 1. clone the repository
 2. cp config.ini.example config.ini
 3. adjust config.ini
 4. sudo docker-compose up -d
 
# Updating
## docker-compose
1. sudo docker-compose down --rmi all
2. git pull
3. sudo docker-compose up -d

# Configuration
## 1. Config file -> Mount to /usr/src/app/config.ini within docker
```ini
[General]
check_interval = 60

[AdKatsDB]
host=
port=3306
user=
pw=
database=

[Destination]
host=
port=21
user=
pw=
# Public webserver URL
public_base=https://bla.com/screenshots/
# Directory for file uploads
upload_dir=/
# Use TLS?
tls=true

[Server1]
host=
port=21
user=
pw=
# Directory on the gameserver FTP where punkbuster screenshots are stored
base_dir=pb/svss

# repeat for each server. ID has to match with the ID in tbl_server
[Server2]
host=
port=21
user=
pw=
base_dir=pb/svss
```
    
# License
This project is free software and licensed under the GPLv3.


``