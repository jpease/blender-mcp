# A "remote" Blender host: headless Blender + Xvfb, with the blender-mcp server
# running beside it over streamable HTTP. See docker-compose.yml for the wiring.
#
# Built from the repository root (compose sets the context) so the server's
# dependencies come from the project's own poetry.lock; Dockerfile.dockerignore
# keeps that context down to the few files the build actually reads.
FROM almalinux:9.8-minimal

ARG BLENDER_MAJOR_MINOR=5.2
ARG BLENDER_VERSION=5.2.1

RUN microdnf install -y epel-release \
    && microdnf install -y --enablerepo=epel \
        xorg-x11-server-Xvfb \
        wget tar xz ca-certificates \
        libX11 libXext mesa-libGL libXrender libXfixes libXi libxkbcommon libSM libICE \
    && microdnf clean all

RUN wget -q "https://download.blender.org/release/Blender${BLENDER_MAJOR_MINOR}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
        -O /tmp/blender.tar.xz \
    && tar xf /tmp/blender.tar.xz -C /opt \
    && rm /tmp/blender.tar.xz \
    && ln -s "/opt/blender-${BLENDER_VERSION}-linux-x64/blender" /usr/local/bin/blender

# An ARG is build-time only. entrypoint.sh needs the same version at run time
# to install the addon into this Blender's addons directory, so bake it into
# the image - otherwise a --build-arg override installs Blender X while the
# entrypoint writes the addon where version Y would look for it, and the addon
# silently never loads.
ENV BLENDER_MAJOR_MINOR=${BLENDER_MAJOR_MINOR}

# The server needs Python 3.13+, which AlmaLinux 9 (and 10) do not package, so
# install a pinned standalone CPython build - the same way Blender is installed
# above, plus a checksum. To bump it, take the release tag, the 3.13.x version,
# and the tarball's line from that release's SHA256SUMS.
ARG PYTHON_VERSION=3.13.15
ARG PYTHON_BUILD_RELEASE=20260901
ARG PYTHON_SHA256=0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461
RUN wget -q "https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_RELEASE}/cpython-${PYTHON_VERSION}%2B${PYTHON_BUILD_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz" \
        -O /tmp/python.tar.gz \
    && echo "${PYTHON_SHA256}  /tmp/python.tar.gz" | sha256sum -c - \
    && tar xzf /tmp/python.tar.gz -C /opt \
    && rm /tmp/python.tar.gz

# The MCP server's runtime dependencies, installed from poetry.lock so the
# container runs exactly the versions CI tests. Poetry itself is only needed to
# read the lock, so it is removed afterwards. The server code is not baked in:
# it runs from the /repo bind mount, so server edits need a restart, not a
# rebuild.
ARG POETRY_VERSION=2.4.1
COPY pyproject.toml poetry.lock /opt/blender-mcp/
RUN /opt/python/bin/python3 -m venv /opt/poetry \
    && /opt/poetry/bin/pip install --no-cache-dir --quiet "poetry==${POETRY_VERSION}" \
    && /opt/python/bin/python3 -m venv /opt/blender-mcp/venv \
    && cd /opt/blender-mcp \
    && VIRTUAL_ENV=/opt/blender-mcp/venv /opt/poetry/bin/poetry install --only main --no-root --no-interaction \
    && rm -rf /opt/poetry /root/.cache

COPY docker/blender/start_server.py /opt/start_server.py
COPY docker/blender/healthcheck.py /opt/healthcheck.py
COPY docker/blender/entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

# Only the MCP server is meant to be reached from outside; Blender's own socket
# (9876) stays on the container's loopback.
EXPOSE 8000

ENTRYPOINT ["/opt/entrypoint.sh"]
