FROM eo2/sfm-base:latest
# @sha256:e68cb98bdc9dc23bbed734f3e507a0ffb866b007dffea038b6af8d88a62150e6
MAINTAINER Frederik Gremler <frederik.gremler@uni-konstanz.de>

RUN apt-get install jq -yq

ENV WORKDIR=/opt/sfm-instagram-harvester
COPY requirements $WORKDIR/requirements
RUN pip install \
                -r $WORKDIR/requirements/common.txt \
                -r $WORKDIR/requirements/release.txt

COPY *.py $WORKDIR/
WORKDIR $WORKDIR

ADD docker/invoke.sh /opt/sfm-setup/
RUN chmod +x /opt/sfm-setup/invoke.sh

CMD ["/opt/sfm-setup/invoke.sh"]
