FROM python:3.11-slim

WORKDIR /app

# Print/log output straight to `docker logs` instead of buffering it
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
