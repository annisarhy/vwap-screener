FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create empty __init__.py files
RUN touch data/__init__.py signals/__init__.py screener/__init__.py notify/__init__.py

CMD ["python", "main.py"]
