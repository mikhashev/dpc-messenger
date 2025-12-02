# D-PC Federation Hub

> **Server-side infrastructure for peer discovery and WebRTC signaling**
> 
> **Status:** Production Ready | **License:** AGPL v3 | **Version:** 0.9.0

The D-PC Federation Hub is a minimalistic server application that provides essential services for the D-PC network: user authentication, peer discovery, and WebRTC signaling. It acts as a "phone book and matchmaker" while never storing or accessing users' private conversations.

---

## 🎯 Purpose & Design Philosophy

The Hub is intentionally designed to be **"dumb"** to respect user privacy:

### What the Hub Does
- ✅ **User Authentication** - OAuth 2.0 (Google, GitHub)
- ✅ **Node Identity Verification** - Validates cryptographic identities
- ✅ **Profile Hosting** - Public expertise profiles only
- ✅ **Peer Discovery** - Search for users by expertise
- ✅ **WebRTC Signaling** - Relay SDP/ICE for P2P setup
- ✅ **Presence** - Track online/offline status
- ✅ **Token Management** - JWT with blacklist support

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
│              D-PC Hub Architecture                      │
└─────────────────────────────────────────────────────────┘

Internet Users
     │
     │ HTTPS/WSS
     ▼
┌───────────────────────────────────────────────────────┐
│  Nginx (Reverse Proxy + SSL Termination)              │
│  - Port 80/443                                        │
│  - Let's Encrypt SSL                                  │
└──────────────────┬────────────────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────────────────┐
│  FastAPI Application                                  │
│  - REST API (OAuth, Profiles, Discovery)              │
│  - WebSocket API (WebRTC Signaling)                   │
│  - Crypto Identity Validation                         │
│  - Token Blacklist Management                         │
│  - Port 8000 (internal)                               │
└──────────────────┬────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────┐
│  PostgreSQL Database                                    │
│  - Users & Authentication                               │
│  - Node Identity (public keys, certificates)            │
│  - Public Profiles                                      │
│  - WebSocket Session State                              │
└─────────────────────────────────────────────────────────┘
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
RATE_LIMIT_ENABLED=true

# Optional: CORS Settings
ALLOWED_ORIGINS="http://localhost:3000,http://localhost:8080"
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

## 📊 API Endpoints

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/login/{provider}` | Initiate OAuth flow (google/github) |
| `GET` | `/auth/{provider}/callback` | OAuth callback handler |
| `POST` | `/token` | Get JWT token (legacy) |
| `POST` | `/logout` | Logout and blacklist token |
| `GET` | `/users/me` | Get current user info |

### Node Identity Registration (NEW in v0.5.0)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/register-node-id` | Register cryptographic node identity |

**Request Body:**
```json
{
  "node_id": "dpc-node-8b066c7f3d7eb627",
  "public_key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----",
  "certificate": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----"
}
```

**Response:**
```json
{
  "node_id": "dpc-node-8b066c7f3d7eb627",
  "verified": true,
  "message": "Node identity successfully registered and verified"
}
```

**Validation Steps:**
1. Certificate format and validity
2. Public key extraction from certificate
3. Node ID derivation from public key
4. Certificate CN matches node_id
5. No duplicate node_id registration

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
| `WebSocket` | `/ws/signal?token=<JWT>` | WebRTC signaling channel |

**Authentication:** JWT token required via query parameter

**Message Types:**
```json
// Client → Server
{
  "type": "signal",
  "target_node_id": "dpc-node-...",
  "payload": {
    "type": "offer|answer|ice-candidate",
    // WebRTC-specific data
  }
}

// Server → Client
{
  "type": "auth_ok",
  "message": "Successfully connected as dpc-node-...",
  "node_id": "dpc-node-..."
}

// Error
{
  "type": "error",
  "message": "Error description",
  "code": "error_code"
}
```

### Health & Monitoring

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Basic status check |
| `GET` | `/health` | Comprehensive health check |

---

## 🔐 Cryptographic Identity System

### How It Works

1. **Client-Side Key Generation**
   - Client generates RSA-4096 key pair
   - Creates self-signed X.509 certificate
   - Derives node_id from public key hash

2. **Registration with Hub**
   - After OAuth login, client sends:
     - `node_id`
     - Public key (PEM format)
     - Certificate (PEM format)

3. **Hub Validation**
   - Validates certificate structure and format
   - Extracts public key from certificate
   - Generates node_id from public key
   - Verifies provided node_id matches computed node_id
   - Checks certificate CN matches node_id
   - Ensures no duplicate registrations

4. **Verified Status**
   - Sets `node_id_verified = true` in database
   - Client can now use full Hub features
   - Other peers can verify identity via public key

### Node ID Format

```
dpc-node-{16_hex_chars}
```

Example: `dpc-node-8b066c7f3d7eb627`

Derived from: `SHA256(public_key)[:16]`

---

## 🛡️ Security Features

### Token Management

**JWT with Blacklist:**
- Tokens are valid for 30 minutes by default
- Logout endpoint blacklists tokens immediately
- Blacklisted tokens rejected until natural expiry
- In-memory blacklist with TTL cleanup

### Rate Limiting

**Default Limits:**
- General endpoints: 60 requests/minute
- Login: 5 requests/minute
- Profile update: 20 requests/minute
- Node registration: 10 requests/minute

Can be disabled by setting `RATE_LIMIT_ENABLED=false`

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

4. **Production Deployment**
   - Always use HTTPS/WSS
   - Deploy behind Nginx with SSL
   - Set up firewall rules
   - Regular security updates

---

## 🌐 Production Deployment

### Option 1: VPS Deployment (Recommended)

```bash
# On your VPS (Ubuntu 22.04+)
cd dpc-hub

# 1. Install dependencies
sudo apt update
sudo apt install docker.io docker-compose python3-poetry -y

# 2. Configure environment
cp .env.example .env
nano .env  # Set production credentials

# 3. Create production docker-compose
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

```bash
# 4. Start services
docker-compose -f docker-compose.prod.yml up -d

# 5. Install and configure Nginx
sudo apt install nginx certbot python3-certbot-nginx -y
sudo nano /etc/nginx/sites-available/dpc-hub
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
# 6. Enable site and get SSL
sudo ln -s /etc/nginx/sites-available/dpc-hub /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
sudo certbot --nginx -d hub.yourdomain.com
```

### Option 2: Platform as a Service

**Heroku:**
```bash
heroku create dpc-hub-prod
heroku addons:create heroku-postgresql:mini
heroku config:set SECRET_KEY="your_secret_key"
git push heroku main
```

**DigitalOcean App Platform:**
1. Connect GitHub repository
2. Select "dpc-hub" directory
3. Add PostgreSQL database
4. Configure environment variables
5. Deploy

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
wscat -c ws://localhost:8000/ws/signal?token=<JWT>
```

**"Node identity validation failed"**
```bash
# Common causes:
# 1. node_id doesn't match public key hash
# 2. Certificate CN doesn't match node_id
# 3. Public key in certificate doesn't match provided public key
# 4. Invalid PEM format

# Check logs for specific validation error
docker-compose logs hub | grep "validation"
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
│   ├── crypto_validation.py # Node identity validation (NEW)
│   ├── settings.py          # Configuration
│   ├── websocket_manager.py # WebSocket connection management
│   │
│   └── alembic/             # Database migrations
│       ├── versions/
│       └── env.py
│
├── tests/                    # Test suite
│   ├── test_auth.py
│   ├── test_crypto_validation.py
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

## 🔄 Database Migrations

```bash
# Create a new migration after model changes
poetry run alembic revision --autogenerate -m "description"

# Apply migrations
poetry run alembic upgrade head

# Rollback last migration
poetry run alembic downgrade -1

# Check current version
poetry run alembic current

# View migration history
poetry run alembic history
```

---

## ⚡ Performance & Scaling

### Horizontal Scaling

1. **Multiple Hub Instances**
   - Deploy multiple Hub instances
   - Use load balancer (Nginx, HAProxy)
   - Share PostgreSQL across instances

2. **Database Optimization**
   - Use connection pooling
   - Add read replicas for search queries
   - Implement caching (Redis) for blacklist

3. **CDN**
   - Serve static assets via CDN
   - Cache API responses when appropriate

### Monitoring

```bash
# Check active WebSocket connections
curl http://localhost:8000/health

# View application logs
docker-compose logs -f hub

# Monitor database performance
docker exec dpc-hub-db psql -U user -d dpc_hub -c "
  SELECT * FROM pg_stat_activity;
"
```

---

## 🤝 Contributing

See the main [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

Hub-specific areas:
- 🔐 Authentication improvements
- 🔍 Crypto validation enhancements
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