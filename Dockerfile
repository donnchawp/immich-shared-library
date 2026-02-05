FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir . && \
    useradd -r -s /usr/sbin/nologin appuser

USER appuser

EXPOSE 8080

CMD ["python", "-m", "src.main"]
