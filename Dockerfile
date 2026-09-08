FROM python:3.12-slim AS tdlib-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    cmake \
    git \
    libssl-dev \
    zlib1g-dev \
    gperf \
    php \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch master https://github.com/tdlib/telegram-bot-api.git

WORKDIR /src/telegram-bot-api
RUN git submodule update --init --recursive

RUN mkdir -p build
WORKDIR /src/telegram-bot-api/build
RUN cmake -DCMAKE_BUILD_TYPE=Release .. \
    && cmake --build . --target install -j"$(nproc)"

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tdlib-builder /usr/local/bin/telegram-bot-api /usr/local/bin/tg_bot_api

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

RUN mkdir -p /app/tg_bot_api_data

CMD ["./entrypoint.sh"]
