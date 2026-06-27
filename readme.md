···bash
docker run -d \
  -p 8880:8880 \
  -v ./data:/app/data \
  --name iptv-toolkit \
  --restart unless-stopped \
  iptv-toolkit:latest
