```bash
docker run -d \
  -p 8880:8880 \
  -v ./data:/app/data \
  --name iptv-toolkit \
  --restart unless-stopped \
  ghcr.io/qbenny/iptv-toolkit:main
