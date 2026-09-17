 FROM python:3.11-slim

# 构建时传入代理（宿主机需要）
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PULL_DATA_CONFIG=/app/config/pull_data.local.yml

WORKDIR /app

# 换 Debian 国内镜像源
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.ustc.edu.cn/debian|g' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://deb.debian.org/debian-security|https://mirrors.ustc.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc default-libmysqlclient-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./pyproject.toml
COPY app ./app
COPY src ./src
RUN pip install --no-cache-dir ".[pacs,rag,mcp]" -i https://mirrors.aliyun.com/pypi/simple/

COPY config ./config
RUN mkdir -p /app/data/fileserver /app/data/dicom_samples

EXPOSE 8000 8001 11112

CMD ["python", "-m", "src.run_service"]
