# Build a Daytona snapshot from a supported Linux desktop image with Python 3 and Git.
ARG DAYTONA_DESKTOP_IMAGE
FROM ${DAYTONA_DESKTOP_IMAGE}
ARG WORKSPACE_USER
ARG WORKER_STATE_DIR
USER root
COPY --chmod=0555 sandbox_worker.py /opt/lotusbuild/sandbox_worker.py
RUN test -n "$WORKSPACE_USER" && test -n "$WORKER_STATE_DIR" && \
    command -v git && command -v python3 && \
    install -d -m 0700 -o "$WORKSPACE_USER" "$WORKER_STATE_DIR"
USER ${WORKSPACE_USER}
