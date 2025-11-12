# GitHub OAuth Setup Guide

This guide explains how to set up and test GitHub authentication for D-PC Messenger.

## Overview

D-PC Hub supports two OAuth providers:
- **Google OAuth** (required)
- **GitHub OAuth** (optional)

GitHub authentication is useful for developers who prefer to authenticate with their GitHub account instead of Google.

---

## Setting Up GitHub OAuth App

### 1. Create a GitHub OAuth App

1. Go to [GitHub Developer Settings](https://github.com/settings/developers)
2. Click **"New OAuth App"** (or "Register a new application")
3. Fill in the application details:

   **Application name:** `D-PC Messenger` (or your preferred name)

   **Homepage URL:**
   - Development: `http://localhost:8000`
   - Production: Your Hub's public URL (e.g., `https://hub.example.com`)

   **Application description:** `Privacy-first P2P messaging with AI collaboration`

   **Authorization callback URL:**
   - Development: `http://localhost:8000/auth/github/callback`
   - Production: `https://your-hub-domain.com/auth/github/callback`

4. Click **"Register application"**
5. After creation, note down the **Client ID**
6. Click **"Generate a new client secret"** and note down the **Client Secret**

⚠️ **Important:** Keep your Client Secret secure! Never commit it to version control.

---

### 2. Configure the Hub

#### Development Setup

1. Navigate to the Hub directory:
   ```bash
   cd dpc-hub
   ```

2. Copy the example environment file if you haven't already:
   ```bash
   cp .env.example .env
   ```

3. Add your GitHub credentials to `.env`:
   ```bash
   # GitHub OAuth credentials (optional)
   GITHUB_CLIENT_ID="your_actual_github_client_id"
   GITHUB_CLIENT_SECRET="your_actual_github_client_secret"
   ```

4. Restart the Hub server:
   ```bash
   poetry run uvicorn dpc_hub.main:app --reload
   ```

5. Check the logs to confirm GitHub OAuth is registered:
   ```
   INFO: GitHub OAuth provider registered
   ```

#### Production Setup

For production, use environment variables instead of the `.env` file:

```bash
export GITHUB_CLIENT_ID="your_github_client_id"
export GITHUB_CLIENT_SECRET="your_github_client_secret"
```

Or configure them in your deployment platform (Docker, Kubernetes, etc.).

---

## User Interface

### OAuth Provider Selection

The D-PC Messenger client UI displays two login buttons in the sidebar:

```
┌─────────────────────────────────┐
│ Connect to Hub for WebRTC and  │
│ discovery                       │
├─────────────────────────────────┤
│  🔵 Google     ⚫ GitHub        │
└─────────────────────────────────┘
```

**Buttons:**
- **Google button** (blue, left) - Primary authentication provider
- **GitHub button** (black, right) - Alternative authentication provider

**User Flow:**
1. User sees both buttons when not connected to Hub
2. User clicks preferred provider button
3. Browser opens to OAuth authorization page
4. User authorizes application
5. Browser redirects back with tokens
6. UI updates to show "Connected to Hub"

---

## Configuration

### Auto-Connect Settings

The client can automatically connect to the Hub on startup using a configured default provider.

**Configuration File:** `~/.dpc/config.ini`

```ini
[hub]
# Enable/disable auto-connect on startup
auto_connect = true  # default: true

[oauth]
# Default provider for auto-connect
default_provider = github  # options: 'google' or 'github' (default: google)
```

**Behavior:**

| Setting | Result |
|---------|--------|
| `auto_connect = true` + `default_provider = google` | Auto-connects with Google on startup (default) |
| `auto_connect = true` + `default_provider = github` | Auto-connects with GitHub on startup |
| `auto_connect = false` | Starts offline; user must click login button manually |

**Use Cases:**

1. **GitHub-first workflow:**
   ```ini
   [hub]
   auto_connect = true
   [oauth]
   default_provider = github
   ```
   - Client always uses GitHub for authentication
   - No need to click button on startup

2. **Manual control:**
   ```ini
   [hub]
   auto_connect = false
   ```
   - Client starts in offline mode
   - User chooses Google or GitHub via UI buttons
   - Useful for testing or switching between providers

3. **Default (Google):**
   ```ini
   [hub]
   auto_connect = true
   [oauth]
   default_provider = google
   ```
   - Standard behavior, auto-connects with Google

**Environment Variable Override:**

You can also set via environment variables:
```bash
export DPC_HUB_AUTO_CONNECT=false
export DPC_OAUTH_DEFAULT_PROVIDER=github
```

---

## Testing GitHub Authentication

### Prerequisites

- Hub server running (`poetry run uvicorn dpc_hub.main:app --reload`)
- PostgreSQL database running (`docker-compose up -d`)
- Database migrations applied (`poetry run alembic upgrade head`)

### Test Flow

#### 1. Test from Browser (Quick Test)

```bash
# Open in browser:
http://localhost:8000/login/github
```

**Expected behavior:**
1. Redirects to GitHub authorization page
2. Shows "Authorize [App Name]" with requested permissions
3. After approval, redirects to `http://127.0.0.1:8080/callback?access_token=...&refresh_token=...`
4. If client is not running, you'll see a connection error (this is expected)

**Verify email scope:**
- GitHub will show: "This application will be able to read your email addresses"

#### 2. Test from Client (Full Flow)

```bash
# Terminal 1: Start Hub
cd dpc-hub
poetry run uvicorn dpc_hub.main:app --reload

# Terminal 2: Start Client Backend
cd dpc-client/core
poetry run python run_service.py

# Terminal 3: Start Client Frontend
cd dpc-client/ui
npm run tauri dev
```

**In the Client UI:**
1. In the sidebar, find the "Connect to Hub for WebRTC and discovery" section
2. Click the **"GitHub"** button (black button with GitHub icon)
   - Alternatively, click **"Google"** for Google authentication
3. Browser opens → Authorize on GitHub
4. Browser redirects back → Client receives tokens
5. Client automatically registers node ID with Hub
6. Status shows "Connected to Hub"

#### 3. Verify Database Entry

```bash
# Connect to database
docker exec -it dpc_hub_db psql -U user -d dpc_hub

# Check user was created
SELECT id, email, provider, node_id_verified, created_at
FROM users
WHERE provider = 'github';

# Should show your GitHub email with provider='github'
```

#### 4. Test with Private Email

GitHub users can hide their email addresses. The implementation handles this:

1. Go to [GitHub Email Settings](https://github.com/settings/emails)
2. Check **"Keep my email addresses private"**
3. Test login flow again

**Expected behavior:**
- System fetches emails from `GET /user/emails` endpoint
- Finds primary verified email
- Falls back to any verified email if no primary
- Login succeeds with verified email

---

## Troubleshooting

### Issue: "Could not retrieve access token from GitHub"

**Cause:** Invalid Client ID/Secret or authorization code expired

**Solution:**
1. Verify credentials in `.env` match GitHub OAuth App settings
2. Check callback URL matches exactly (no trailing slash)
3. Try logging in again (authorization codes expire quickly)

---

### Issue: "Could not retrieve email from GitHub"

**Cause:** No verified email address on GitHub account

**Solution:**
1. Go to [GitHub Email Settings](https://github.com/settings/emails)
2. Add and verify an email address
3. Make sure at least one email is marked as verified
4. Try logging in again

---

### Issue: "GitHub OAuth provider not registered"

**Cause:** Missing or invalid GitHub credentials

**Solution:**
1. Check `.env` file has both `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET`
2. Restart Hub server after adding credentials
3. Check Hub logs for registration confirmation

---

### Issue: "GitHub authentication is not configured on this Hub"

**Cause:** User clicked GitHub button but Hub doesn't have GitHub credentials configured

**What happens:**
1. User clicks "GitHub" button in client UI
2. Browser opens to Hub's `/login/github` endpoint
3. Hub returns HTTP 503 error with message

**Solution:**
- **For Hub administrators:** Add GitHub OAuth credentials to `.env` and restart
- **For users:** Use Google authentication instead, or contact Hub administrator

---

### Issue: "Redirect URI mismatch"

**Cause:** Callback URL in GitHub OAuth App doesn't match request

**Solution:**
1. In [GitHub OAuth App settings](https://github.com/settings/developers)
2. Verify Authorization callback URL is **exactly**: `http://localhost:8000/auth/github/callback`
3. No trailing slash, no extra parameters
4. For production: Update to your domain (e.g., `https://hub.example.com/auth/github/callback`)

---

## Security Considerations

### Email Scope

The implementation requests `user:email` scope, which grants:
- Read access to email addresses
- No write access
- No access to private repositories or code

### Private Emails

GitHub's "Keep my email private" feature is fully supported:
- The Hub can still retrieve verified emails via API
- Users can maintain privacy settings
- Only verified emails are accepted for authentication

### Token Handling

- GitHub access token is only used during callback
- Token is **not stored** in the database
- JWT tokens are generated for Hub authentication
- Original OAuth token is discarded after user creation

### Rate Limiting

GitHub API has rate limits:
- 5,000 requests/hour for authenticated requests
- Login attempts are rate-limited by Hub (5/minute per IP)

---

## Client-Side Implementation

### Using GitHub Authentication

```python
from dpc_client_core.hub_client import HubClient

# Initialize client
hub_client = HubClient(hub_url="http://localhost:8000")

# Login with GitHub
await hub_client.login(provider="github")

# Check authentication status
is_logged_in = await hub_client.is_logged_in()
```

### Programmatic Testing

```python
import asyncio
from dpc_client_core.hub_client import HubClient

async def test_github_auth():
    hub = HubClient(hub_url="http://localhost:8000")

    print("Starting GitHub OAuth flow...")
    success = await hub.login(provider="github")

    if success:
        print("✓ Successfully authenticated with GitHub")

        # Verify login
        logged_in = await hub.is_logged_in()
        print(f"✓ Login verified: {logged_in}")

        # Get user info
        profile = await hub.get_my_profile()
        print(f"✓ User email: {profile['email']}")
    else:
        print("✗ GitHub authentication failed")

asyncio.run(test_github_auth())
```

---

## API Endpoints

### Login Initiation
```
GET /login/github
```
Redirects to GitHub authorization page.

### OAuth Callback
```
GET /auth/github/callback?code={authorization_code}&state={state}
```
Handles GitHub callback, creates/updates user, returns JWT tokens.

### User Info
```
GET /users/me
Authorization: Bearer {access_token}
```
Returns authenticated user information.

---

## Comparison: Google vs GitHub

| Feature | Google OAuth | GitHub OAuth |
|---------|-------------|--------------|
| **Status** | Required | Optional |
| **Email Access** | Direct (in userinfo) | API call required |
| **Private Emails** | N/A | Supported via `/user/emails` |
| **Scope** | `openid email profile` | `user:email` |
| **OIDC Support** | Yes | No |
| **Implementation** | OpenID Connect | OAuth 2.0 |

---

## Production Deployment

### Environment Variables

```bash
# GitHub OAuth (optional but recommended)
GITHUB_CLIENT_ID=ghp_your_production_client_id
GITHUB_CLIENT_SECRET=your_production_secret

# Update allowed origins for production
ALLOWED_ORIGINS=https://your-app.com,tauri://localhost
```

### Callback URL

Update GitHub OAuth App callback URL to production domain:
```
https://hub.yourdomain.com/auth/github/callback
```

### Multiple Environments

Create separate OAuth Apps for each environment:
- Development: `http://localhost:8000/auth/github/callback`
- Staging: `https://staging-hub.example.com/auth/github/callback`
- Production: `https://hub.example.com/auth/github/callback`

Use environment-specific credentials in each deployment.

---

## FAQ

**Q: Can users log in with both Google and GitHub?**

A: Yes! Users with the same email can authenticate with either provider. Accounts are identified by email address, not by provider. The Hub automatically updates the provider field to reflect the most recently used authentication method.

**Example:**
1. User registers with Google → Database: `{email: "user@example.com", provider: "google"}`
2. Same user logs in with GitHub → Database: `{email: "user@example.com", provider: "github"}`
3. Provider field is updated to "github" (most recent login)

**Note:** The provider field always reflects the last authentication method used.

**Q: What happens if a user changes their GitHub email?**

A: The Hub identifies users by email. If the primary email changes on GitHub, the next login will create a new Hub account (if email doesn't match existing account).

**Q: Can I disable Google and use only GitHub?**

A: No, Google OAuth is currently required. GitHub is an optional secondary provider. This may change in future versions.

**Q: Does GitHub authentication work offline?**

A: Initial authentication requires internet access. After successful login, tokens are cached locally (encrypted) for offline mode until they expire.

**Q: Can I use the same email address on multiple devices?**

A: **No, not currently.** The Hub implementation currently supports **one device per user account** (one device per email address).

**How it works:**
- Each device generates a unique cryptographic identity (`node_id`) derived from its RSA key pair
- When you log in, the Hub associates your email with your device's `node_id`
- If you log in from a second device with the same email, it will overwrite the first device's `node_id`

**Example:**
```
Device 1 (Laptop):  dpc-node-aaaa1111 + user@example.com ✅ Registered
Device 2 (Desktop): dpc-node-bbbb2222 + user@example.com ✅ Registered
                    Device 1 is now "orphaned" ❌
```

**Workarounds:**
- **Different email addresses:** Use a different email/OAuth account for each device
- **Direct P2P only:** Direct TLS connections work without Hub (no WebRTC though)
- **Development/Testing:** Use multiple OAuth credentials (see [CONFIGURATION.md](./CONFIGURATION.md))

**Why this limitation:**
- Each device has unique cryptographic keys (security by design)
- Private keys never leave the device
- Current Hub database schema: one `node_id` per user

**Future plans:**
Multi-device support would require Hub database schema changes to support one-to-many relationships (one user, multiple devices). This is a potential future enhancement but not currently implemented.

**Important:** The `default_provider` config (Google vs GitHub) is about choosing which OAuth account to authenticate with, **not** about managing multiple devices. It controls which email gets associated with your single device's node_id.

---

## Additional Resources

- [GitHub OAuth Documentation](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
- [GitHub API: User Emails](https://docs.github.com/en/rest/users/emails)
- [OAuth 2.0 RFC](https://datatracker.ietf.org/doc/html/rfc6749)

---

## Need Help?

If you encounter issues:
1. Check Hub logs: `poetry run uvicorn dpc_hub.main:app --reload` (shows detailed errors)
2. Verify database state: Check `users` table for entries
3. Test with browser first: `http://localhost:8000/login/github`
4. Enable debug mode: Set `DEBUG=true` in `.env`

For bugs or feature requests, please open an issue on the GitHub repository.
