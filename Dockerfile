# Cypress is required at runtime because the API launches Cypress test runs.
FROM cypress/included:13.17.0

USER root

# The base image already contains this Cypress version; avoid downloading it
# again while installing the workspace's Node dependencies.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    CYPRESS_INSTALL_BINARY=0

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

# Install dependencies before copying application code to preserve Docker's
# layer cache when only source files change.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY cypress-workspace/package.json cypress-workspace/package-lock.json ./cypress-workspace/
RUN npm ci --prefix ./cypress-workspace

COPY . .

EXPOSE 8000

# The Cypress base image has a Cypress entrypoint; clear it so this container
# starts the FastAPI service instead.
ENTRYPOINT []
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
