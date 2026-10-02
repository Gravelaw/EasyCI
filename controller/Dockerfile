FROM jenkins/jenkins:2.580.1-jdk21@sha256:a660310e39ade10631f774bacd5219de767dfd08947ea5c32a739b5e5bb382c1
# Install only the reviewed closure, checking every downloaded plugin against the lock.
# No plugin resolver selects newer transitive versions during the build.
COPY dependency-lock.json /usr/share/jenkins/ref/dependency-lock.json
COPY controller/install-plugins.sh /tmp/install-plugins.sh
COPY controller/plugins-download.tsv /tmp/locked-plugins.tsv
RUN bash /tmp/install-plugins.sh
COPY controller/jenkins.yaml /usr/share/jenkins/ref/jenkins.yaml
COPY controller/bootstrap.yaml /usr/share/jenkins/ref/bootstrap.yaml
ENV CASC_JENKINS_CONFIG=/usr/share/jenkins/ref/jenkins.yaml
# Preserve the setup wizard. No administrator, API token, or agent is provisioned.
