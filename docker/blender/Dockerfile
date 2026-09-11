# A "remote" Blender host: headless Blender + Xvfb, with the blender-mcp server
# running beside it over streamable HTTP. See docker-compose.yml for the wiring.
#
# Built from the repository root (compose sets the context) so the server's
# dependencies come from the project's own pyproject.toml; Dockerfile.dockerignore
# keeps that context down to the few files the build actually reads.
FROM ghcr.io/astral-sh/uv:0.9.16 AS uv

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

# The MCP server's dependencies, read from pyproject.toml so this list can never
# drift from the project's. The server code itself is not baked in: it runs
# from the /repo bind mount, so server edits need a restart, not a rebuild.
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
COPY pyproject.toml /opt/blender-mcp/pyproject.toml
RUN uv venv --python 3.13 /opt/blender-mcp/venv \
    && uv pip install --python /opt/blender-mcp/venv -r /opt/blender-mcp/pyproject.toml \
    && rm -rf /root/.cache/uv

COPY docker/blender/start_server.py /opt/start_server.py
COPY docker/blender/healthcheck.py /opt/healthcheck.py
COPY docker/blender/entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

# Only the MCP server is meant to be reached from outside; Blender's own socket
# (9876) stays on the container's loopback.
EXPOSE 8000

ENTRYPOINT ["/opt/entrypoint.sh"]
