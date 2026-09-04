FROM python:3.14

# User and Group ID of the account used for execution
ARG UID=4000
ARG GID=4000

LABEL maintainer="Hedius @ gitlab.com/hedius" \
      description="image for PunkBusterScreenshotProcessor" \
      version="1.0.0"

# account for execution of script
RUN groupadd -r -g $GID  pythonRun && \
    useradd -r -g pythonRun -u $UID pythonRun

COPY --chown=pythonRun:pythonRun src /usr/src/app

WORKDIR /usr/src/app

# Install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends libmariadb3 libmariadb-dev gcc && \
    pip3 install --no-cache-dir -r requirements.txt && \
    apt-get purge -y --auto-remove libmariadb-dev gcc && \
    rm -rf /var/lib/apt/lists/*

RUN chown pythonRun:pythonRun -R /usr/src/app

USER pythonRun:pythonRun

CMD ["python3", "punkbuster_screenshot_processor.py", "-c", "config.ini"]
