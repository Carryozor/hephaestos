FROM python:3.12.8-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY deploy/entrypoint.py .
COPY deploy/create-user.py .
USER 1000:1000
CMD ["python", "entrypoint.py"]
