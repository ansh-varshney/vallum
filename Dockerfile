FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY control ./control
RUN useradd --create-home --uid 10001 vallum
USER vallum

# APP_MODULE=control.main:app runs the control room (the status site) from the same image
CMD ["sh", "-c", "exec uvicorn ${APP_MODULE:-app.main:app} --host 0.0.0.0 --port ${PORT} --no-access-log"]
