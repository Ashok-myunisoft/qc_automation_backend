FROM cypress/base:13.17.0

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        freetds-dev \
        pkg-config \
        python3 \
        python3-dev \
        python3-venv \
    && python3 -m venv "$VIRTUAL_ENV" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY cypress-workspace/package.json cypress-workspace/package-lock.json ./cypress-workspace/
RUN npm ci --prefix ./cypress-workspace

COPY . .

EXPOSE 8000

ENTRYPOINT []
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
