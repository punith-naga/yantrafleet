# Yantrika loopback demo image.
#
# Build from the REPO ROOT (the Dockerfile lives in docker/ but needs the
# whole repo as its context):
#
#   docker build -f docker/Dockerfile -t yantrafleet .
#
# Run (Linux — the stack binds 127.0.0.1 on ephemeral ports, so host
# networking is the supported way to reach the console; see docker/README.md):
#
#   docker run --rm --init --network host yantrafleet
#
# and open the console URL printed in the READY line.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Non-root user; yantraops writes its state file to $HOME.
RUN useradd --create-home --uid 1000 yantra
WORKDIR /app

# Third-party deps first, for build-cache-friendly layers.
COPY copilot/requirements.txt /app/copilot/requirements.txt
RUN pip install -r /app/copilot/requirements.txt

# The repo itself, then the six first-party packages (editable, matching the
# documented dev setup — the source of truth stays /app).
COPY . /app
RUN pip install -e core -e sim -e connector -e detector -e notifier -e ops \
    && chown -R yantra:yantra /app

USER yantra

# The orchestrator picks ephemeral 127.0.0.1 ports at startup, so there is no
# fixed port to EXPOSE; `--network host` (Linux) is the supported access path.

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "yantraops", "status"]

# --no-open: there is no browser in the container.
CMD ["python", "-m", "yantraops", "up", "--loopback", "--no-open"]
