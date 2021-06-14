FROM eo2/sfm-base:latest
# @sha256:e68cb98bdc9dc23bbed734f3e507a0ffb866b007dffea038b6af8d88a62150e6
MAINTAINER Frederik Gremler <frederik.gremler@uni-konstanz.de>

RUN apt-get install jq -yq

ADD . /opt/sfm-instagram-harvester/
WORKDIR /opt/sfm-instagram-harvester

RUN pip install -r requirements/common.txt -r requirements/release.txt

ADD docker/invoke.sh /opt/sfm-setup/
RUN chmod +x /opt/sfm-setup/invoke.sh

CMD ["/opt/sfm-setup/invoke.sh"]
