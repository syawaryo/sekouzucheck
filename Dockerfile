# --- Stage 1: Build frontend ---
FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python backend ---
FROM python:3.12-slim
WORKDIR /app

# ODA File Converter (DWG→DXF). Qt app, needs xvfb+xauth on headless Linux.
# URL may need updating: https://www.opendesign.com/guestfiles/oda_file_converter
ARG ODA_DEB_URL=https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_27.1.deb
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth wget ca-certificates \
      libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
      libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-sync1 \
      libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxcb-cursor0 \
      libglu1-mesa libsm6 \
      libfontconfig1 libfreetype6 libdbus-1-3 libegl1 libgl1 libnss3 \
      libxrender1 libxext6 libxi6 libxtst6 libxrandr2 libxdamage1 \
      libxcomposite1 libxfixes3 libasound2 libpulse0 libxss1; \
    wget --no-verbose -O /tmp/oda.deb \
      --referer="https://www.opendesign.com/guestfiles/oda_file_converter" \
      --user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" \
      "$ODA_DEB_URL"; \
    apt-get install -y --no-install-recommends /tmp/oda.deb; \
    rm /tmp/oda.deb; \
    which ODAFileConverter; \
    ODABIN=$(find /usr/bin/ODAFileConverter_* -maxdepth 1 -type f -name ODAFileConverter 2>/dev/null | head -1); \
    echo "ODA binary: $ODABIN"; \
    ldd "$ODABIN" | grep -i "not found" && (echo "FATAL: missing libs" && exit 1) || echo "All libs OK"; \
    apt-get purge -y wget; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sleeve_checker/ ./sleeve_checker/
COPY api.py ./
COPY dxf_output/ ./dxf_output/

# Copy frontend build output
COPY --from=frontend-build /app/frontend/dist ./static

EXPOSE 10000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "10000"]
