ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
LABEL dspilot.terminus.base-image="${BASE_IMAGE}"
USER root
RUN apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*
