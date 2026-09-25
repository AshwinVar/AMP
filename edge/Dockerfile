# AMP Edge Gateway — for a plant that would rather run a container than a service.
#
# NOT the only way to run this, and not the default one. Many plant PCs are a
# Windows box under a desk with no Docker and no appetite for one; for those,
# `pip install -r requirements.txt` and a service wrapper is the shorter path
# and INSTALL.md covers it. This exists because the sites that DO have container
# tooling find it much easier to hand a customer an image than a Python
# environment.
#
# Build (from the repository root):
#   docker build -t amp-edge:latest edge/
#
# Run:
#   docker run -d --name amp-edge --restart unless-stopped \
#     --network host \
#     -v /opt/amp-edge/gateway.yaml:/etc/amp-edge/gateway.yaml:ro \
#     -v amp-edge-queue:/var/lib/amp-edge \
#     --env-file /opt/amp-edge/.env \
#     amp-edge:latest run /etc/amp-edge/gateway.yaml
#
# --network host because the gateway has to reach a PLC on the plant LAN, which
# a bridge network usually cannot. The container opens NO ports of its own.
#
# The queue is a VOLUME. Without it, a container restart throws away every
# reading that had not yet reached AMP — which is the one thing the local buffer
# exists to prevent.

FROM python:3.11-slim

# curl is not installed and is not wanted: a gateway on a control network should
# have the smallest possible toolbox if it is ever used as a foothold.
RUN useradd --create-home --shell /usr/sbin/nologin ampedge \
    && mkdir -p /var/lib/amp-edge /etc/amp-edge \
    && chown -R ampedge:ampedge /var/lib/amp-edge

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ampedge/ ./ampedge/

# Never root. The gateway needs to open outbound sockets and write one file;
# nothing it does requires privilege, and a container on a control network is
# exactly where that matters.
USER ampedge

# The queue's default location inside the image. Override in the config if you
# mount somewhere else.
ENV AMP_EDGE_QUEUE=/var/lib/amp-edge/queue.db

ENTRYPOINT ["python", "-m", "ampedge"]
CMD ["--help"]
