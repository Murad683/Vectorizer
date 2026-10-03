FROM python:3.11-slim

WORKDIR /app

# Graphviz system binary — required by the layout engine (pygraphviz wrapper).
RUN apt-get update && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
