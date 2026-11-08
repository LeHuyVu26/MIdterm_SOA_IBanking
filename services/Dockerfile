FROM python:3.12-slim
ARG SERVICE
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ${SERVICE}/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]