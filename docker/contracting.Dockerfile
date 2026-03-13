FROM python:3.11.9-bullseye

RUN apt-get update && apt-get install -y \
    git \
    libhdf5-dev

WORKDIR /usr/src/app

RUN pip install pytest
RUN pip install parameterized

CMD ["tail", "-f", "/dev/null"]
