FROM jenkins/inbound-agent:jdk21@sha256:6a31d728c22ad74adbb6b9a1a210eb9ec2599f644b95edd7f8a3b8a71d20772c
USER root
# These OS tools are installed only during the explicitly approved image build.
# Capture dpkg inventory and derived image digest; apt versions are not yet locked.
RUN apt-get update && apt-get install --no-install-recommends -y python3 git \
    && dpkg-query -W > /opt/local-ci-os-packages.txt \
    && rm -rf /var/lib/apt/lists/*
COPY scripts /opt/local-ci/scripts
COPY projects /opt/local-ci/projects
COPY schemas /opt/local-ci/schemas
COPY agent/start.sh /opt/local-ci/start.sh
RUN mkdir -p /home/jenkins/cache /home/jenkins/releases \
    && chown jenkins:jenkins /home/jenkins/cache /home/jenkins/releases \
    && chmod 0750 /home/jenkins/cache /home/jenkins/releases
USER jenkins
ENTRYPOINT ["bash", "/opt/local-ci/start.sh"]
