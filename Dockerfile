FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY run.py countries.json ./
COPY fotoman ./fotoman

ENV FOTOMAN_DATA=/data
ENV FOTOMAN_CACHE=/cache
EXPOSE 8000

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
