FROM python:3.12-slim

# Install dependencies for building telegram-bot-api
RUN apt-get update && apt-get install -y \
    cmake g++ git libssl-dev zlib1g-dev \
    libreadline-dev libncurses5-dev gperf \
    supervisor curl \
    && rm -rf /var/lib/apt/lists/*

# Build Telegram Local Bot API Server
RUN git clone --recursive --depth 1 https://github.com/tdlib/telegram-bot-api.git /opt/telegram-bot-api \
    && cd /opt/telegram-bot-api \
    && mkdir build \
    && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release .. \
    && cmake --build . --target telegram-bot-api -j$(nproc) \
    && mv telegram-bot-api /usr/local/bin/ \
    && rm -rf /opt/telegram-bot-api

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY . .

# Create downloads directory
RUN mkdir -p downloads

# Copy supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 3000 8081

ENV LOCAL_API_URL=http://localhost:8081

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
