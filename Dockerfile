FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DELTA_CONTRACTS_DIR=/app/vendor/contracts PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY vendor/contracts/ vendor/contracts/
COPY src/ src/
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin delta
USER 1000
EXPOSE 8080
CMD ["uvicorn", "delta_api.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
