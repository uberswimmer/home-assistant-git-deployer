# Version history:
# 1.3.0 - 2026-08-24 - Flattened the runtime image to a single deployer.py implementation and removed obsolete versioned wrapper modules.
# 1.2.0 - 2026-08-23 - Added deployer_v120.py to the local add-on image and executable permissions.
# 1.1.0 - 2026-08-23 - Added deployer_v110.py to the local add-on image and executable permissions.
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    git \
    openssh-client \
    python3 \
    py3-requests

COPY run.sh /run.sh
COPY deployer.py /deployer.py
COPY github_known_hosts /github_known_hosts
RUN chmod 0755 /run.sh /deployer.py \
    && chmod 0644 /github_known_hosts

CMD ["/run.sh"]
