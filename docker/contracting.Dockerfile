FROM python:3.14-bookworm

RUN apt-get update && apt-get install -y \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

RUN pip install pytest
RUN pip install parameterized

CMD ["tail", "-f", "/dev/null"]
