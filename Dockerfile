FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
# 1 worker supaya scheduler hanya jalan sekali; threads untuk request paralel
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "300", "main:app"]
