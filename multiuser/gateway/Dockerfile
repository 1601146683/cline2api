FROM python:3.12-alpine

WORKDIR /app
COPY gateway.py /app/gateway.py

ENV UPSTREAM=http://cline-proxy:3457 \
    DB_PATH=/data/gateway.db \
    AUTH_MODE=auto \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).status==200 else 1)"

ENTRYPOINT ["python", "/app/gateway.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--wait-upstream", "90"]
