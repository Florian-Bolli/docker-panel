FROM python:3.12-alpine

WORKDIR /app
COPY app.py .

ENV PYTHONUNBUFFERED=1 \
    PANEL_HOST=0.0.0.0

EXPOSE 9001

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9001/', timeout=2)"

CMD ["python", "app.py"]
