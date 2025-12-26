FROM python:3.11-slim

WORKDIR /app

# Copy requirements from the app/ folder inside the repo
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the entire repo so /app/app/... exists
COPY . /app

CMD ["python", "/app/app/main.py"]