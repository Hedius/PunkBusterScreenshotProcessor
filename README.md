# PunkBuster Screenshot Processor

[![pipeline status](https://gitlab.com/e4gl/punkbusterscreenshotprocessor/badges/main/pipeline.svg)](https://gitlab.com/e4gl/punkbusterscreenshotprocessor/-/commits/main)
[![Discord](https://img.shields.io/discord/388757799875903489.svg?colorB=7289DA&label=Discord&logo=Discord&logoColor=7289DA&style=flat-square)](https://discord.e4gl.com/)
<!-- [![Docker Pulls](https://img.shields.io/docker/pulls/hedius/bf4metricsloggger.svg?style=flat-square)](https://hub.docker.com/r/hedius/bf4metricslogger/) -->

# What does this do
1. Pull punkbuster screenshots from several servers over FTP.
2. Convert them to JPEG and crop them to remove the integrated text information.
   1. Set `compress = false` in the config to keep the original PNGs instead.
3. Upload them to a destination FTP/FTPs or SFTP server with a random guid.
   1. The root folder contains the cropped version. Furthermore, the full
      screenshot is placed in the subfolder **full**.
4. Scan every screenshot for cheat ESP overlays before it is compressed, and
   store the verdict with the screenshot. See [ESP detection](#esp-detection).
5. Store the date in a table in the PRoCon DB.
6. Removes screenshots after X days. (default 60).
   1. The tuple is deleted from the DB and both files are deleted.
   2. The screenshots of banned players are preserved.
   3. Screenshots flagged for a suspected ESP overlay are preserved as well.

This can be used for viewing the screenshots of your players.
You can then integrate this into your BFACP or to your discord like we did.

If you have any questions pls contact Hedius#0001 on Discord.

# Setup
## 1. Create the required table
```sql
create table e4gl_screenshots
(
    screenshot_id int unsigned auto_increment
        primary key,
    server_id     smallint unsigned not null,
    player_id     int unsigned      not null,
    timestamp     datetime          not null,
    guid          char(36)          null,
    url           varchar(128)      not null,
    full_url      varchar(128)      null,
    esp_status    enum ('clean', 'flagged', 'capture_failed') null,
    esp_score     decimal(5, 2)     null,
    esp_reason    varchar(128)      null,
    esp_model     varchar(32)       null,
    constraint e4gl_screenshots_guid_uindex
        unique (guid),
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

create index e4gl_screenshots_esp_status_timestamp_index
    on e4gl_screenshots (esp_status, timestamp);

```

### Upgrading an existing table
The four `esp_*` columns were added later. Apply this **before** starting the new
version - the insert writes to columns that have to exist already:
```sql
alter table e4gl_screenshots
    add column esp_status enum ('clean', 'flagged', 'capture_failed') null,
    add column esp_score  decimal(5, 2) null,
    add column esp_reason varchar(128)  null,
    add column esp_model  varchar(32)   null;

create index e4gl_screenshots_esp_status_timestamp_index
    on e4gl_screenshots (esp_status, timestamp);
```
The columns are appended at the end of the table, so anything reading it with
`select *` keeps working.

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
# how often to check the source FTP servers for new
# files
check_interval = 60

# remove old screenshots after x days
# screenshots of banned players are never removed
cleanup_after_days = 60

# timezone of the game servers / PRoCon DB.
# defaults to Europe/Berlin if not set.
timezone = Europe/Berlin

# convert the screenshots to JPEG to save disk space.
# set to false to keep the original PNGs.
# defaults to true if not set.
compress = true

[ESPDetection]
# Scan every screenshot for cheat ESP overlays before it is compressed.
enabled = true

# Override the threshold fitted into the model (0-100). Lower catches more
# cheaters and produces more false positives.
#threshold =

# Path to the fitted model. Defaults to the one shipped with the code.
#model_path =

[AdKatsDB]
host=
port=3306
user=
pw=
database=

[Destination]
# Destination type: ftp or sftp
type=ftp
host=
port=21
user=
# Public webserver URL
public_base=https://bla.com/screenshots/
# Directory for file uploads
upload_dir=/

# --- ftp options ---
pw=
# Use TLS?
tls=true

# --- sftp options ---
# Password login. Optional if a private key is configured below.
#pw=
# Path to a private key file. Optional if a password is set above.
#private_key_path=
# Passphrase for the private key. Optional.
#private_key_passphrase=
# Path to a known_hosts style file containing the server's host key. Required.
# Generate it with e.g.:
#   ssh-keyscan -p <port> -H <host> > known_hosts
#host_key_path=

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

## 2. SFTP destination
When `type=sftp` is set in `[Destination]`, the private key file (`private_key_path`) and the
known_hosts style host key file (`host_key_path`) must be mounted into the container next to
`config.ini` (see `docker-compose.yml`), and must be readable by the container's runtime user
(UID/GID `4000` by default, see `Dockerfile`).

# ESP detection
Every screenshot is scanned for cheat ESP overlays - player boxes, corner
brackets, skeletons, name/distance labels and health bars - on the cropped frame
**before** it is compressed, because JPEG artifacts cost detection accuracy.

The result is written to the screenshot's row:

| column       | meaning                                                             |
|--------------|---------------------------------------------------------------------|
| `esp_status` | `clean`, `flagged` or `capture_failed`. `NULL` means it was not scanned. |
| `esp_score`  | 0-100 suspicion score. `NULL` for failed captures.                   |
| `esp_reason` | the signals that fired, e.g. `green_run, palette_outlier`.            |
| `esp_model`  | content hash of the model that produced the score.                   |

`capture_failed` means PunkBuster captured an all-black frame. That is evidence
of neither cheating nor innocence, so it is recorded rather than scored.

A score is stored for **every** screenshot, not only for flagged ones, so the
threshold can be reconsidered later without re-processing anything:
```sql
-- the 50 most suspicious screenshots of the last week, whatever the threshold was
select url, esp_score, esp_reason from e4gl_screenshots
where timestamp > date_sub(now(), interval 7 day)
order by esp_score desc limit 50;
```

Flagged screenshots are logged at `WARNING` with their URL and are exempt from
the cleanup, so they survive until an admin has reviewed them.

## Retuning the model
`src/image_processing/esp_detection.py` doubles as the tool that fits the model.
It needs a folder of clean screenshots and a folder of known ESP screenshots:
```bash
python src/image_processing/esp_detection.py compare --ok samples_ok --esp samples_esp
python src/image_processing/esp_detection.py tune --ok samples_ok --esp samples_esp --target-fp 0.04
python src/image_processing/esp_detection.py eval --ok samples_ok --esp samples_esp --out results
```
`compare` sweeps combiner configurations, `tune` fits and saves the model, and
`eval` reports cross-validated recall and false-positive rates and sorts every
screenshot into `results/` for review. `scan` scores loose files.

**Commit `src/image_processing/esp_model.json` after every retune.** `tune`
overwrites it in place, and the `esp_model` hash stored on each screenshot is
only resolvable if the matching file is in git.

# License
This project is free software and licensed under the GPLv3.

Disclaimer. Part of this project has been coded with AI tools to save me some time (SFTP, ESP detection).

``