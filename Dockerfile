# Use official Python slim image
FROM python:3.11-slim

WORKDIR /app

# Copy deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shim `uvicorn` so a start command containing a literal "${PORT:-8000}"
# (Railway custom start commands run without a shell) still binds correctly.
COPY docker/uvicorn-shim.sh /usr/local/bin/uvicorn-shim
RUN mv /usr/local/bin/uvicorn /usr/local/bin/uvicorn-real \
 && cp /usr/local/bin/uvicorn-shim /usr/local/bin/uvicorn \
 && chmod +x /usr/local/bin/uvicorn /usr/local/bin/uvicorn-real

# Copy app code
COPY . .

# Railway/Render inject PORT at runtime; app/__main__.py reads it in Python,
# so no shell expansion is required (exec-form CMD also forwards signals).
CMD ["python", "-m", "app"]
