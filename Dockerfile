FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# .env (secrets) and orders.db (persistent state) are mounted as volumes at
# run time, not baked into the image — see docker-compose.yml.
CMD ["python", "bot.py"]
