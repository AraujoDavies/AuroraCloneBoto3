FROM python:3.11-slim-bookworm

WORKDIR /app

ARG TERRAFORM_VERSION=1.17.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        make \
        mydumper \
        curl \
        ca-certificates \
        unzip \
    && curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
        -o /tmp/terraform.zip \
    && unzip -o /tmp/terraform.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/terraform \
    && rm -f /tmp/terraform.zip /usr/local/bin/LICENSE.txt \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

CMD ["bash"]
