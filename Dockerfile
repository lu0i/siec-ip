FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping iproute2 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY skaner.py .
EXPOSE 8080
CMD ["python3", "skaner.py", "--db", "/data/urzadzenia.db"]
