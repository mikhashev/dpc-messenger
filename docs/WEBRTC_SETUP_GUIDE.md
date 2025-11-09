# WebRTC Production Setup Guide

> **Deploy D-PC Hub for internet-wide P2P connections**

This guide covers production deployment of the D-PC Federation Hub with full WebRTC support, SSL/TLS, and production-grade configuration.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [VPS Setup](#vps-setup)
4. [Hub Deployment](#hub-deployment)
5. [Nginx Configuration](#nginx-configuration)
6. [SSL/TLS Setup](#ssltls-setup)
7. [WebRTC Configuration](#webrtc-configuration)
8. [Testing & Validation](#testing--validation)
9. [Monitoring](#monitoring)
10. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│              Production Hub Architecture                    │
└─────────────────────────────────────────────────────────────┘

Internet
    │
    │ HTTPS/WSS (443)
    ▼
┌─────────────────────────────────┐
│  Nginx Reverse Proxy            │
│  - SSL Termination              │
│  - WebSocket Upgrade            │
│  - Rate Limiting                │
│  - Static Caching               │
└────────────┬────────────────────┘
             │ HTTP (8000)
             ▼
┌─────────────────────────────────┐
│  FastAPI Application            │
│  - REST API                     │
│  - WebSocket Signaling          │
│  - OAuth Authentication         │
│  - Node Identity Validation     │
└────────────┬────────────────────┘
             │ PostgreSQL
             ▼
┌─────────────────────────────────┐
│  PostgreSQL Database            │
│  - User accounts                │
│  - Node identities              │
│  - Public profiles              │
└─────────────────────────────────┘
```

---

## Prerequisites

### Hardware Requirements

**Minimum (Testing):**
- CPU: 1 core
- RAM: 1 GB
- Storage: 10 GB
- Bandwidth: 100 Mbps

**Recommended (Production):**
- CPU: 2+ cores
- RAM: 2+ GB
- Storage: 20+ GB
- Bandwidth: 1 Gbps

### Software Requirements

- **OS:** Ubuntu 22.04 LTS or later
- **Access:** Root or sudo privileges
- **Network:** Public IP address
- **Domain:** Recommended (e.g., `hub.example.com`)

### Services Needed

- **OAuth Provider:** Google Cloud Console account
- **DNS:** Domain name with A record pointing to your VPS
- **Email:** For Let's Encrypt SSL notifications

---

## VPS Setup

### Step 1: Create VPS

Choose a provider:
- **DigitalOcean** - Droplets starting at $6/month
- **Linode** - VPS starting at $5/month
- **Vultr** - Cloud Compute starting at $6/month
- **Hetzner** - Most affordable in Europe
- **AWS EC2** - Enterprise-grade (more complex)

### Step 2: Initial Server Configuration

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install essential tools
sudo apt install -y \
    curl \
    git \
    vim \
    ufw \
    fail2ban \
    htop

# Create non-root user (if not exists)
sudo adduser dpc
sudo usermod -aG sudo dpc

# Switch to new user
su - dpc
```

### Step 3: Configure Firewall

```bash
# Set up UFW firewall
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow SSH (change 22 if using custom port)
sudo ufw allow 22/tcp

# Allow HTTP and HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Allow WebRTC UDP range (optional, for future TURN server)
sudo ufw allow 49152:65535/udp

# Enable firewall
sudo ufw enable

# Verify status
sudo ufw status verbose
```

### Step 4: Install Docker

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add user to docker group
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt install docker-compose -y

# Verify installation
docker --version
docker-compose --version

# Log out and back in for group changes to take effect
exit
su - dpc
```

### Step 5: Install Poetry (Python Package Manager)

```bash
# Install Poetry
curl -sSL https://install.python-poetry.org | python3 -

# Add to PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Verify installation
poetry --version
```

---

## Hub Deployment

### Step 1: Clone Repository

```bash
cd ~
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger/dpc-hub
```

### Step 2: Configure Environment

```bash
# Copy example environment file
cp .env.example .env

# Generate secure secret key
SECRET_KEY=$(openssl rand -hex 32)

# Edit configuration
nano .env
```

**Production `.env` Configuration:**

```bash
# ============================================================================
# REQUIRED: Security
# ============================================================================

# Generate with: openssl rand -hex 32
SECRET_KEY="paste_your_generated_secret_key_here"

# JWT settings
ALGORITHM="HS256"
ACCESS_TOKEN_EXPIRE_MINUTES=30

# ============================================================================
# REQUIRED: Database
# ============================================================================

DATABASE_URL="postgresql+asyncpg://dpc_user:STRONG_PASSWORD@localhost:5432/dpc_hub"

# ============================================================================
# REQUIRED: OAuth Providers
# ============================================================================

# Google OAuth (get from https://console.cloud.google.com)
GOOGLE_CLIENT_ID="your_app.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="your_secret_here"

# GitHub OAuth (optional)
GITHUB_CLIENT_ID=""
GITHUB_CLIENT_SECRET=""

# ============================================================================
# OPTIONAL: Server Settings
# ============================================================================

HOST="0.0.0.0"
PORT="8000"
DEBUG=false
APP_NAME="D-PC Hub"
APP_VERSION="0.5.0"

# ============================================================================
# OPTIONAL: CORS
# ============================================================================

# Add your frontend domains
ALLOWED_ORIGINS="https://hub.example.com,https://example.com"

# ============================================================================
# OPTIONAL: Rate Limiting
# ============================================================================

RATE_LIMIT_ENABLED=true
```

**Important:** Replace `STRONG_PASSWORD` with a secure password!

### Step 3: Set Up Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable "Google+ API"
4. Create OAuth 2.0 credentials:
   - Application type: Web application
   - Authorized redirect URIs:
     - `https://hub.example.com/auth/google/callback`
   - Copy Client ID and Secret to `.env`

### Step 4: Create Production Docker Compose

Create `docker-compose.prod.yml`:

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:16
    container_name: dpc-hub-db
    environment:
      POSTGRES_USER: dpc_user
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: dpc_hub
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    restart: unless-stopped
    networks:
      - dpc-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dpc_user"]
      interval: 10s
      timeout: 5s
      retries: 5

  hub:
    build: .
    container_name: dpc-hub
    depends_on:
      postgres:
        condition: service_healthy
    env_file:
      - .env
    ports:
      - "127.0.0.1:8000:8000"
    restart: unless-stopped
    networks:
      - dpc-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  postgres_data:

networks:
  dpc-network:
    driver: bridge
```

### Step 5: Create Dockerfile

Create `Dockerfile`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN pip install poetry

# Copy project files
COPY pyproject.toml poetry.lock ./
COPY dpc_hub ./dpc_hub
COPY alembic.ini ./
COPY dpc_hub/alembic ./dpc_hub/alembic

# Install dependencies
RUN poetry config virtualenvs.create false \
    && poetry install --no-dev --no-interaction --no-ansi

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run migrations and start server
CMD poetry run alembic upgrade head && \
    uvicorn dpc_hub.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Step 6: Deploy with Docker

```bash
# Build and start services
docker-compose -f docker-compose.prod.yml up -d --build

# Check status
docker-compose -f docker-compose.prod.yml ps

# View logs
docker-compose -f docker-compose.prod.yml logs -f hub

# Verify Hub is running
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "version": "0.5.0",
  "database": "connected",
  "websocket_connections": 0
}
```

---

## Nginx Configuration

### Step 1: Install Nginx

```bash
sudo apt install nginx -y
sudo systemctl enable nginx
sudo systemctl start nginx
```

### Step 2: Create Site Configuration

```bash
sudo nano /etc/nginx/sites-available/dpc-hub
```

**Nginx Configuration:**

```nginx
# Rate limiting zones
limit_req_zone $binary_remote_addr zone=general:10m rate=60r/m;
limit_req_zone $binary_remote_addr zone=auth:10m rate=5r/m;

# Upstream (backend)
upstream dpc_hub {
    server 127.0.0.1:8000;
    keepalive 32;
}

# HTTP -> HTTPS redirect
server {
    listen 80;
    listen [::]:80;
    server_name hub.example.com;
    
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    
    location / {
        return 301 https://$server_name$request_uri;
    }
}

# HTTPS server
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name hub.example.com;
    
    # SSL certificates (will be configured by certbot)
    ssl_certificate /etc/letsencrypt/live/hub.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hub.example.com/privkey.pem;
    
    # SSL settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384';
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;
    ssl_stapling on;
    ssl_stapling_verify on;
    
    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    
    # Logging
    access_log /var/log/nginx/dpc-hub-access.log;
    error_log /var/log/nginx/dpc-hub-error.log;
    
    # Client settings
    client_max_body_size 10M;
    
    # WebSocket support
    location /ws/ {
        proxy_pass http://dpc_hub;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket timeout
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        
        # No rate limiting for WebSocket
    }
    
    # API endpoints with rate limiting
    location /auth/ {
        limit_req zone=auth burst=10 nodelay;
        
        proxy_pass http://dpc_hub;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    
    # All other locations
    location / {
        limit_req zone=general burst=20 nodelay;
        
        proxy_pass http://dpc_hub;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

**Replace `hub.example.com` with your actual domain!**

### Step 3: Enable Site

```bash
# Test configuration
sudo nginx -t

# Enable site
sudo ln -s /etc/nginx/sites-available/dpc-hub /etc/nginx/sites-enabled/

# Remove default site
sudo rm /etc/nginx/sites-enabled/default

# Restart Nginx
sudo systemctl restart nginx
```

---

## SSL/TLS Setup

### Step 1: Install Certbot

```bash
sudo apt install certbot python3-certbot-nginx -y
```

### Step 2: Obtain SSL Certificate

```bash
# Get certificate (follow prompts)
sudo certbot --nginx -d hub.example.com

# Test auto-renewal
sudo certbot renew --dry-run
```

### Step 3: Configure Auto-Renewal

```bash
# Certbot should auto-create renewal cron job
# Verify it exists:
sudo systemctl status certbot.timer

# Manual renewal test
sudo certbot renew --dry-run
```

---

## WebRTC Configuration

### STUN/TURN Servers

The client is configured to use:

1. **STUN** (default):
   - `stun.l.google.com:19302`
   - `stun1.l.google.com:19302`

2. **TURN** (fallback):
   - OpenRelay: `turn:openrelay.metered.ca:80`

### Optional: Deploy Your Own TURN Server

For maximum privacy and reliability:

```bash
# Install coturn
sudo apt install coturn -y

# Configure coturn
sudo nano /etc/turnserver.conf
```

**Basic turnserver.conf:**

```conf
listening-port=3478
fingerprint
lt-cred-mech
use-auth-secret
static-auth-secret=YOUR_SECRET_KEY
realm=hub.example.com
total-quota=100
stale-nonce=600
cert=/etc/letsencrypt/live/hub.example.com/cert.pem
pkey=/etc/letsencrypt/live/hub.example.com/privkey.pem
no-cli
```

```bash
# Enable and start coturn
sudo systemctl enable coturn
sudo systemctl start coturn

# Update firewall
sudo ufw allow 3478/tcp
sudo ufw allow 3478/udp
sudo ufw allow 49152:65535/udp
```

**Update client configuration** in `dpc-client/core/dpc_client_core/webrtc_peer.py`:

```python
ice_servers = [
    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
    RTCIceServer(
        urls=["turn:hub.example.com:3478"],
        username="username",
        credential="YOUR_SECRET_KEY"
    ),
]
```

---

## Testing & Validation

### Step 1: Test Hub Health

```bash
curl https://hub.example.com/health
```

Expected:
```json
{
  "status": "healthy",
  "version": "0.5.0",
  "database": "connected"
}
```

### Step 2: Test OAuth Flow

1. Visit `https://hub.example.com/login/google`
2. Complete OAuth
3. Should redirect with token

### Step 3: Test WebSocket Connection

```bash
# Install wscat
npm install -g wscat

# Test WebSocket (need valid JWT token)
wscat -c "wss://hub.example.com/ws/signal?token=YOUR_JWT_TOKEN"
```

### Step 4: Test Client Connection

1. Start client with production Hub URL
2. Login via OAuth
3. Verify node registration
4. Connect to another peer
5. Send messages

### Step 5: Test WebRTC NAT Traversal

```bash
# Run connectivity test
cd dpc-client/core
poetry run python tests/test_turn_connectivity.py
```

---

## Monitoring

### Step 1: Set Up Log Monitoring

```bash
# Hub logs
docker-compose -f docker-compose.prod.yml logs -f hub

# Nginx logs
sudo tail -f /var/log/nginx/dpc-hub-access.log
sudo tail -f /var/log/nginx/dpc-hub-error.log

# System logs
sudo journalctl -u nginx -f
```

### Step 2: Set Up Health Monitoring

Create `/home/dpc/monitor-hub.sh`:

```bash
#!/bin/bash

HEALTH_URL="https://hub.example.com/health"
ALERT_EMAIL="your@email.com"

response=$(curl -s -o /dev/null -w "%{http_code}" $HEALTH_URL)

if [ $response != "200" ]; then
    echo "Hub is down! Status: $response" | mail -s "Hub Alert" $ALERT_EMAIL
fi
```

```bash
# Make executable
chmod +x /home/dpc/monitor-hub.sh

# Add to crontab (every 5 minutes)
crontab -e
*/5 * * * * /home/dpc/monitor-hub.sh
```

### Step 3: Resource Monitoring

```bash
# Install monitoring tools
sudo apt install htop iotop -y

# Check resources
htop
docker stats
```

---

## Troubleshooting

### Hub Not Starting

```bash
# Check logs
docker-compose -f docker-compose.prod.yml logs hub

# Common issues:
# 1. DATABASE_URL incorrect
# 2. PostgreSQL not ready
# 3. Port 8000 already in use

# Check PostgreSQL
docker exec dpc-hub-db psql -U dpc_user -d dpc_hub -c "SELECT 1;"
```

### SSL Certificate Issues

```bash
# Check certificate
sudo certbot certificates

# Renew manually
sudo certbot renew

# Check Nginx config
sudo nginx -t
```

### WebSocket Connection Failed

```bash
# Check Nginx WebSocket config
sudo nginx -t

# Test WebSocket directly
wscat -c "wss://hub.example.com/ws/signal?token=TEST"

# Check firewall
sudo ufw status
```

### WebRTC Connection Timeout

```bash
# Test STUN connectivity
nslookup stun.l.google.com

# Check client logs for ICE state
# Look for "ICE connection state: failed"

# Common causes:
# 1. UDP blocked by firewall
# 2. TURN server unavailable
# 3. Symmetric NAT (both peers)
```

### High Memory Usage

```bash
# Check PostgreSQL
docker stats dpc-hub-db

# Optimize PostgreSQL
# Edit docker-compose.prod.yml, add under postgres:
    command: postgres -c max_connections=50 -c shared_buffers=256MB
```

---

## Backup & Recovery

### Database Backup

```bash
# Create backup script
cat > /home/dpc/backup-db.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/home/dpc/backups"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

docker exec dpc-hub-db pg_dump -U dpc_user dpc_hub > $BACKUP_DIR/dpc_hub_$DATE.sql

# Keep only last 7 days
find $BACKUP_DIR -name "dpc_hub_*.sql" -mtime +7 -delete
EOF

chmod +x /home/dpc/backup-db.sh

# Add to crontab (daily at 2 AM)
crontab -e
0 2 * * * /home/dpc/backup-db.sh
```

### Restore Database

```bash
# Restore from backup
docker exec -i dpc-hub-db psql -U dpc_user dpc_hub < backup.sql
```

---

## Scaling

### Horizontal Scaling

```bash
# Deploy multiple Hub instances behind load balancer
# Use shared PostgreSQL database
# Example: HAProxy configuration

frontend http_front
    bind *:80
    default_backend http_back

backend http_back
    balance roundrobin
    server hub1 hub1.example.com:8000 check
    server hub2 hub2.example.com:8000 check
```

### Vertical Scaling

```bash
# Increase Docker resources
# Edit docker-compose.prod.yml, add under hub:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
```

---

## Security Checklist

- [ ] Strong `SECRET_KEY` generated
- [ ] Database uses strong password
- [ ] UFW firewall configured
- [ ] Fail2ban installed
- [ ] SSL certificate installed
- [ ] HTTPS enforced
- [ ] Rate limiting enabled
- [ ] Regular backups configured
- [ ] Log monitoring active
- [ ] System updates scheduled
- [ ] OAuth redirect URIs restricted
- [ ] CORS origins configured

---

## Maintenance

### Regular Tasks

**Daily:**
- Check logs for errors
- Monitor resource usage
- Verify backups

**Weekly:**
- Review security logs
- Check SSL expiration
- Update dependencies

**Monthly:**
- System updates
- Database optimization
- Backup testing

### Update Procedure

```bash
# Pull latest code
cd ~/dpc-messenger
git pull

# Rebuild containers
cd dpc-hub
docker-compose -f docker-compose.prod.yml down
docker-compose -f docker-compose.prod.yml up -d --build

# Run migrations
docker-compose -f docker-compose.prod.yml exec hub poetry run alembic upgrade head
```

---

## Support

For issues:
- **Documentation:** [GitHub Wiki](https://github.com/mikhashev/dpc-messenger/wiki)
- **Issues:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Discussions:** [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Email:** legoogmiha@gmail.com

---

<div align="center">

**[⬅️ Back to Main README](../README.md)** | **[📖 Quick Start](./QUICK_START.md)** | **[🔧 Technical Overview](./README_WEBRTC_INTEGRATION.md)**

*Part of the D-PC Messenger project*

**Production deployment successful? Let us know!**

</div>