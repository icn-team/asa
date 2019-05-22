FROM ubuntu:bionic
LABEL maintainer="Mauro Sardara <msardara@cisco.com>"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qy \
 && apt-get upgrade -qy \
 && apt-get install -y \
    bridge-utils \
    iproute2 \
    python3-ipy \
    python3-pip \
    python3-pyroute2 \
    socat \
    qemu-kvm \
    curl \
 && apt-get -y autoremove \
 && rm -rf /var/lib/apt/lists/*
 
COPY *.py /
COPY *.conf /

EXPOSE 22 161/udp 830 5000-5003 10000-10099
HEALTHCHECK CMD ["/healthcheck.py"]
#ENTRYPOINT ["/launch.py"]

ENTRYPOINT ["sleep", "10000000000000000"]