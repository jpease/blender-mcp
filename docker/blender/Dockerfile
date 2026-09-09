# Headless Blender + Xvfb, for exercising the BlenderMCP addon end-to-end
# without a GUI session. See docker-compose.yml for how this is wired up.
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

COPY start_server.py /opt/start_server.py
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

EXPOSE 9876

ENTRYPOINT ["/opt/entrypoint.sh"]
