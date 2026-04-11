###################################################
# Stage: backend-base
###################################################
FROM python:3.13-slim AS backend-base
WORKDIR /app/backend

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

###################################################
# Stage: backend-dev
###################################################
FROM backend-base AS backend-dev

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

EXPOSE 8000
# app.main:app because main.py lives in backend/app/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

###################################################
# Stage: backend-prod
###################################################
FROM backend-base AS backend-prod

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]