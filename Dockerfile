FROM python:3.11-slim

WORKDIR /app
COPY wheels/ ./wheels/
RUN pip install --no-cache-dir --no-index --find-links=./wheels psycopg2-binary && rm -rf ./wheels
COPY pyfanuc.py .
COPY collector.py .

CMD ["python", "-u", "collector.py"]
