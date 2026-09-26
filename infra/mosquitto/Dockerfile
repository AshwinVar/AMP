# AMP's production MQTT broker.
#
# Deployed on Railway as its own service, built from this directory. It contains
# no AMP code and imports nothing from backend/ or edge/ — it is infrastructure
# AMP talks to, in the same category as Postgres, and ADR-0040's separation
# applies to it for the same reason.
#
# Build/run locally:
#   docker build -t amp-mosquitto infra/mosquitto
#   docker run --rm -p 1883:1883 -e AMP_BACKEND_PASSWORD=local-only amp-mosquitto
#
# (docker-compose.yml deliberately does NOT use this image. The local stack runs
# an anonymous broker so a developer can `mosquitto_pub` at it without a
# credential; this one has no anonymous path at all. The comment at the top of
# that file's mosquitto service explains the trade.)

# Pinned, not `:2`. The `2` tag has already moved from the 2.0.x line to 2.1.x,
# and a broker that silently changes major-minor between two deploys is a
# broker whose ACL semantics might have changed between two deploys.
FROM eclipse-mosquitto:2.0.22

# The base image sets no USER, so this container runs as root, and mosquitto
# drops to the `mosquitto` user itself once it has read its config.
# entrypoint.sh depends on both facts; they were read out of the image's own
# config in the registry rather than assumed.
USER root

# THIS DOES NOT COVER /mosquitto/data, AND THAT IS NOT A MISTAKE.
# The base image declares /mosquitto/data and /mosquitto/log as VOLUMEs, and
# Docker discards what a later build step writes into an inherited volume path.
# So this chown sticks for /mosquitto/config and is thrown away for the other
# two. entrypoint.sh chowns them again at RUNTIME, which is the only moment
# that works — and is also the only moment that can fix the volume Railway
# mounts over /mosquitto/data, which arrives owned by root.
# openssl's CLI, for the private CA entrypoint.sh generates on the volume. The
# image already links libssl, but the `openssl` COMMAND is a separate package
# and is not in the base image.
RUN apk add --no-cache openssl

RUN mkdir -p /mosquitto/config/conf.d /mosquitto/data /mosquitto/log \
 && chown -R mosquitto:mosquitto /mosquitto

COPY mosquitto.conf /mosquitto/config/mosquitto.conf
COPY entrypoint.sh  /usr/local/bin/amp-mosquitto

# Belt and braces on the line endings. .gitattributes pins this file to LF, but
# a checkout on a Windows machine with a global core.autocrlf, or an editor that
# "helpfully" normalises on save, produces a script whose shebang ends in \r —
# and the container then fails with the famously unhelpful
# "/usr/local/bin/amp-mosquitto: not found" while the file plainly exists.
RUN sed -i 's/\r$//' /usr/local/bin/amp-mosquitto \
 && chmod 0755 /usr/local/bin/amp-mosquitto

# Documentation, not a publication: Railway routes by its own configuration, and
# reaching this port from outside the private network needs a TCP proxy that is
# enabled in the dashboard. See README.md.
EXPOSE 1883

ENTRYPOINT ["/usr/local/bin/amp-mosquitto"]
