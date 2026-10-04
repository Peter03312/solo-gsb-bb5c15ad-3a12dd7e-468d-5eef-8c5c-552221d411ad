FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies at image build time.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Tables are created automatically on application startup (see app.db.init_db).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
