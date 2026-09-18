# Use official Python slim image
FROM python:3.11-slim

WORKDIR /app

# Copy deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Railway/Render inject PORT at runtime; app/__main__.py reads it in Python,
# so no shell expansion is required (exec-form CMD also forwards signals).
CMD ["python", "-m", "app"]
