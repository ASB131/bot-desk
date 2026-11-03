FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
RUN python -m unittest discover -s tests

RUN mkdir -p /app/data/uploads
VOLUME /app/data
EXPOSE 23435

CMD ["python", "-m", "app.main"]
