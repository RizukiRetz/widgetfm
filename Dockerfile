# Gunakan Python 3.11 versi slim (lebih kecil, tanpa GUI)
FROM python:3.11-slim

# Set working directory di dalam container
WORKDIR /app

# Install dependencies dulu (layer ini di-cache, tidak perlu rebuild jika kode berubah)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# Copy semua file project (kecuali yang ada di .dockerignore)
# image_cache.json ikut disertakan agar tidak perlu scrape Last.FM di server
COPY . .

# Jalankan script dengan flag -u (unbuffered output agar log langsung terlihat)
CMD ["python", "-u", "upstats.py"]
