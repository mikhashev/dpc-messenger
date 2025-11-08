# D-PC Federation Hub

> **Server-side infrastructure for peer discovery and WebRTC signaling**
> 
> **Status:** Production Ready | **License:** AGPL v3

The D-PC Federation Hub is a minimalistic server application that provides essential services for the D-PC network: user authentication, peer discovery, and WebRTC signaling. It acts as a "phone book and matchmaker" while never storing or accessing users' private conversations.

---

## 🎯 Purpose & Design Philosophy

The Hub is intentionally designed to be **"dumb"** to respect user privacy:

### What the Hub Does
- ✅ **User Authentication** - OAuth 2.0 (Google, GitHub)
- ✅ **Profile Hosting** - Public expertise profiles only
- ✅ **Peer Discovery** - Search for users by expertise
- ✅ **WebRTC Signaling** - Relay SDP/ICE for P2P setup
- ✅ **Presence** - Track online/offline status

### What the Hub Does NOT Do
- ❌ **No Message Storage** - Messages go directly peer-to-peer
- ❌ **No Context Access** - Personal data stays on client devices
- ❌ **No Content Inspection** - End-to-end encrypted channels
- ❌ **No Relationship Graph** - Doesn't track who talks to whom

**Privacy Guarantee:** Even if the Hub is compromised, attackers gain no access to message content or personal contexts.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│              D-PC Hub Architecture                       │
└─────────────────────────────────────────────────────────┘

Internet Users
     │
     │ HTTPS/WSS
     ▼
┌─────────────────────────────────────────────────────┐
│  Nginx (Reverse Proxy + SSL Termination)           │
│  - Port 80/443                                      │
│  - Let's Encrypt SSL                                │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  FastAPI Application                                │
│  - REST API (OAuth, Profiles, Discovery)           │
│  - WebSocket API (WebRTC Signaling)                │
│  - Port 8000 (internal)                             │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│  PostgreSQL Database                                │
│  - Users & Authentication                           │
│  - Public Profiles                                  │
│  - WebSocket Session State                          │
└─────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.12+** with Poetry
- **Docker** (for PostgreSQL)
- **Google OAuth Credentials** (from [Google Cloud Console](https://console.cloud.google.com/))

### Local Development Setup

```bash
cd dpc-hub

# 1. Start PostgreSQL with Docker
docker-compose up -d

# 2. Install Python dependencies
poetry install

# 3. Configure environment variables
cp .env.example .env
nano .env  # Edit with your settings

# 4. Run database migrations
poetry run alembic upgrade head

# 5. Start the server
poetry run uvicorn dpc_hub.main:app --reload
```

The Hub will be available at `http://127.0.0.1:8000`

**API Documentation:** http://127.0.0.1:8000/docs

---

## ⚙️ Configuration

### Environment Variables (`.env`)

```bash
# Database Connection
DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/dpc_hub"

# Security
SECRET_KEY="generate_with_openssl_rand_hex_32"
ALGORITHM="HS256"
ACCESS_TOKEN_EXPIRE_MINUTES=30

# OAuth Providers
GOOGLE_CLIENT_ID="your_app.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="your_secret"

# Optional: GitHub OAuth
GITHUB_CLIENT_ID="your_github_client_id"
GITHUB_CLIENT_SECRET="your_github_secret"

# Optional: Server Settings
HOST="0.0.0.0"
PORT="8000"
```

### Getting OAuth Credentials

#### Google OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable "Google+ API"
4. Go to "Credentials" → "Create Credentials" → "OAuth 2.0 Client ID"
5. Application type: "Web application"
6. Authorized redirect URIs:
   - Development: `http://localhost:8000/auth/google/callback`
   - Production: `https://yourdomain.com/auth/google/callback`
7. Copy Client ID and Client Secret to `.env`

#### GitHub OAuth

1. Go to GitHub Settings → Developer settings → OAuth Apps
2. Click "New OAuth App"
3. Application name: "D-PC Hub (YourDomain)"
4. Homepage URL: `https://yourdomain.com`
5. Authorization callback URL: `https://yourdomain.com/auth/github/callback`
6. Copy Client ID and Client Secret to `.env`

---

## 🗄️ Database Management

### Using Docker

```bash
# Start PostgreSQL
docker-compose up -d

# View logs
docker-compose logs -f postgres

# Stop database
docker-compose down

# Stop and remove data
docker-compose down -v
```

### Using External PostgreSQL

If you prefer using an external PostgreSQL instance:

```bash
# Install PostgreSQL 16+
sudo apt install postgresql-16

# Create database
sudo -u postgres createdb dpc_hub

# Create user
sudo -u postgres createuser -P dpcuser

# Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE dpc_hub TO dpcuser;"

# Update .env
DATABASE_URL="postgresql+asyncpg://dpcuser:password@localhost:5432/dpc_hub"
```

### Migrations

```bash
# Create a new migration
poetry run alembic revision --autogenerate -m "description"

# Apply migrations
poetry run alembic upgrade head

# Rollback one migration
poetry run alembic downgrade -1

# View migration history
poetry run alembic history

# View current version
poetry run alembic current
```

---

## 🌐 Production Deployment

### Option 1: VPS with Docker (Recommended)

**Requirements:**
- Ubuntu 22.04+ VPS
- 1 GB RAM minimum
- Domain with DNS pointing to VPS
- Ports 80, 443 open

**Step-by-Step:**

```bash
# 1. Connect to your VPS
ssh root@your-server-ip

# 2. Install Docker & Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
apt install docker-compose -y

# 3. Clone repository
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger/dpc-hub

# 4. Configure production environment
cp .env.example .env
nano .env  # Set production credentials

# 5. Create production docker-compose
nano docker-compose.prod.yml
```

**docker-compose.prod.yml:**
```yaml
version: '3.8'

services:
  postgres:
    image: postgres:16
    container_name: dpc-hub-db
    environment:
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: dpc_hub
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped
    networks:
      - dpc-network

  hub:
    build: .
    container_name: dpc-hub
    depends_on:
      - postgres
    env_file:
      - .env
    ports:
      - "8000:8000"
    restart: unless-stopped
    networks:
      - dpc-network

volumes:
  postgres_data:

networks:
  dpc-network:
    driver: bridge
```

**Dockerfile:**
```dockerfile
FROM python:3.12-slim

WORKDIR /app

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

# Run migrations and start server
CMD poetry run alembic upgrade head && \
    uvicorn dpc_hub.main:app --host 0.0.0.0 --port 8000
```

```bash
# 6. Start services
docker-compose -f docker-compose.prod.yml up -d

# 7. Install Nginx
apt install nginx certbot python3-certbot-nginx -y

# 8. Configure Nginx
nano /etc/nginx/sites-available/dpc-hub
```

**Nginx Configuration:**
```nginx
server {
    server_name hub.yourdomain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket support
        proxy_read_timeout 86400;
    }
}
```

```bash
# 9. Enable site and get SSL
ln -s /etc/nginx/sites-available/dpc-hub /etc/nginx/sites-enabled/
nginx -t
systemctl restart nginx
certbot --nginx -d hub.yourdomain.com

# 10. Set up auto-renewal for SSL
certbot renew --dry-run
```

### Option 2: Platform as a Service

#### Heroku

```bash
# Install Heroku CLI
curl https://cli-assets.heroku.com/install.sh | sh

# Login and create app
heroku login
heroku create dpc-hub-prod

# Add PostgreSQL
heroku addons:create heroku-postgresql:mini

# Set environment variables
heroku config:set SECRET_KEY="your_secret_key"
heroku config:set GOOGLE_CLIENT_ID="your_client_id"
heroku config:set GOOGLE_CLIENT_SECRET="your_secret"

# Deploy
git push heroku main

# Run migrations
heroku run poetry run alembic upgrade head
```

#### DigitalOcean App Platform

1. Connect GitHub repository
2. Select "dpc-hub" directory
3. Add PostgreSQL database
4. Configure environment variables
5. Deploy

---

## 📊 API Endpoints

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/login/{provider}` | Initiate OAuth flow |
| `GET` | `/auth/{provider}/callback` | OAuth callback |
| `POST` | `/token` | Get JWT token |
| `GET` | `/users/me` | Get current user info |

### Profile Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/profile` | Get own profile |
| `PUT` | `/profile` | Update profile |
| `GET` | `/profile/{node_id}` | Get user's profile |
| `DELETE` | `/profile` | Delete account |

### Discovery

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/discovery/search` | Search users by expertise |

**Query Parameters:**
- `q` - Search query (required)
- `min_level` - Minimum proficiency level (optional, 1-5)

### WebRTC Signaling

| Protocol | Endpoint | Description |
|----------|----------|-------------|
| `WebSocket` | `/ws/signal` | WebRTC signaling channel |

**Authentication:** JWT token required via query parameter or first message

---

## 🔒 Security

### Best Practices

1. **Strong SECRET_KEY**
   ```bash
   # Generate secure key
   openssl rand -hex 32
   ```

2. **Database Security**
   - Use strong passwords
   - Restrict network access
   - Enable SSL connections in production

3. **OAuth Configuration**
   - Use HTTPS redirect URIs only
   - Restrict OAuth scopes to minimum needed
   - Rotate credentials periodically

4. **Rate Limiting**
   - Implement at Nginx level
   - Protect against brute force attacks

5. **Monitoring**
   - Set up logging (see below)
   - Monitor failed login attempts
   - Alert on unusual traffic patterns

### Security Headers

Add to Nginx configuration:

```nginx
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "no-referrer-when-downgrade" always;
add_header Content-Security-Policy "default-src 'self' http: https: data: blob: 'unsafe-inline'" always;
```

---

## 📈 Monitoring & Logging

### Application Logs

```bash
# Development
poetry run uvicorn dpc_hub.main:app --log-level debug

# Production (with Docker)
docker-compose logs -f hub

# Production (systemd)
journalctl -u dpc-hub -f
```

### Database Logs

```bash
# Docker
docker-compose logs -f postgres

# Native PostgreSQL
tail -f /var/log/postgresql/postgresql-16-main.log
```

### Metrics & Monitoring

#### Prometheus + Grafana

```yaml
# Add to docker-compose.prod.yml
  prometheus:
    image: prom/prometheus
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    
  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
```

---

## 🧪 Testing

### API Testing

```bash
# Install test dependencies
poetry install --with dev

# Run tests
poetry run pytest

# With coverage
poetry run pytest --cov=dpc_hub

# Specific test
poetry run pytest tests/test_auth.py
```

### Manual Testing

```bash
# Test health endpoint
curl http://localhost:8000/

# Test authentication
curl http://localhost:8000/login/google

# Test WebSocket
wscat -c ws://localhost:8000/ws/signal?token=YOUR_JWT_TOKEN
```

---

## 🔧 Maintenance

### Backup Database

```bash
# Docker
docker exec dpc-hub-db pg_dump -U user dpc_hub > backup.sql

# Restore
docker exec -i dpc-hub-db psql -U user dpc_hub < backup.sql

# Native PostgreSQL
pg_dump -U dpcuser dpc_hub > backup.sql
psql -U dpcuser dpc_hub < backup.sql
```

### Updating

```bash
# Pull latest code
git pull origin main

# Update dependencies
poetry update

# Run new migrations
poetry run alembic upgrade head

# Restart services
docker-compose -f docker-compose.prod.yml restart hub
```

### Scaling

For high traffic, consider:

1. **Horizontal Scaling**
   - Deploy multiple Hub instances
   - Use load balancer (Nginx, HAProxy)
   - Share PostgreSQL across instances

2. **Database Optimization**
   - Use connection pooling
   - Add read replicas for search queries
   - Implement caching (Redis)

3. **CDN**
   - Serve static assets via CDN
   - Cache API responses when appropriate

---

## 🐛 Troubleshooting

### Common Issues

**"Connection refused" on port 8000**
```bash
# Check if app is running
docker-compose ps

# Check logs
docker-compose logs hub

# Restart
docker-compose restart hub
```

**"database connection failed"**
```bash
# Check PostgreSQL status
docker-compose ps postgres

# Verify DATABASE_URL in .env
# Ensure format: postgresql+asyncpg://user:pass@host:port/db

# Test connection
docker exec dpc-hub-db psql -U user -d dpc_hub -c "SELECT 1;"
```

**"OAuth error: redirect_uri_mismatch"**
```bash
# Verify redirect URI in Google Cloud Console matches:
# Development: http://localhost:8000/auth/google/callback
# Production: https://yourdomain.com/auth/google/callback
```

**"WebSocket connection failed"**
```bash
# Check Nginx WebSocket configuration
# Ensure proxy_http_version 1.1 is set
# Ensure Connection "upgrade" header is set

# Test WebSocket directly
wscat -c ws://localhost:8000/ws/signal
```

---

## 📊 Project Structure

```
dpc-hub/
├── dpc_hub/                  # Main application package
│   ├── __init__.py
│   ├── main.py              # FastAPI app & routes
│   ├── database.py          # Database configuration
│   ├── models.py            # SQLAlchemy models
│   ├── schemas.py           # Pydantic schemas
│   ├── crud.py              # Database operations
│   ├── auth.py              # Authentication logic
│   ├── settings.py          # Configuration
│   │
│   └── alembic/             # Database migrations
│       ├── versions/
│       └── env.py
│
├── tests/                    # Test suite
│   ├── test_auth.py
│   ├── test_profiles.py
│   └── test_signaling.py
│
├── .env.example             # Environment template
├── .gitignore
├── alembic.ini              # Alembic configuration
├── docker-compose.yml       # Development Docker setup
├── docker-compose.prod.yml  # Production Docker setup
├── Dockerfile
├── poetry.lock
├── pyproject.toml           # Dependencies
└── README.md                # This file
```

---

## 🤝 Contributing

See the main [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

Hub-specific areas:
- 🔐 Authentication improvements
- 📊 Performance optimization
- 🧪 Test coverage
- 📝 API documentation
- 🔧 DevOps improvements

---

## 📄 License

This component is licensed under **AGPL v3**. See [LICENSE](../LICENSE.md) for details.

**TL;DR:** You can run, modify, and distribute this software. If you run it as a network service and modify it, you must share your source code with your users.

**Important:** The AGPL network clause ensures that cloud providers can't create proprietary "D-PC Hub as a Service" offerings without contributing back to the community.

---

## 📞 Support

- **Issues:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Discussions:** [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Email:** legoogmiha@gmail.com
- **Documentation:** [Main README](../README.md)

---

<div align="center">

**[⬅️ Back to Main README](../README.md)** | **[📖 API Specification](../specs/hub_api_v1.md)** | **[🚀 Quick Start](../docs/QUICK_START.md)**

*Part of the D-PC Messenger project*

</div>