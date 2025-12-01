# Logging System

D-PC Messenger uses Python's standard library `logging` module for comprehensive logging across all components.

## Overview

**Key Features:**
- Rotating file logs (10MB max, 5 backups)
- Per-module log level configuration
- Console output for development
- File output for production debugging
- Environment variable overrides
- No emojis or decorative prefixes in logs

## Log Files

**Location:** `~/.dpc/logs/dpc-client.log`

**Rotation:** Automatic when file reaches 10MB (keeps 5 backup files)

**Format:**
```
2025-12-01 14:23:45,123 - INFO - dpc_client_core.service - Starting CoreService
2025-12-01 14:23:45,456 - INFO - dpc_client_core.p2p_manager - Direct TLS server listening on 0.0.0.0:8888
```

## Configuration

### Basic Configuration (config.ini)

```ini
[logging]
# Global log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
log_level = INFO

# Console output (true/false)
console_output = true

# Log file path (default: ~/.dpc/logs/dpc-client.log)
log_file = /path/to/custom/log.log

# Per-module log levels (optional)
module_levels = dpc_client_core.p2p_manager:DEBUG,dpc_client_core.webrtc_peer:DEBUG
```

### Environment Variable Overrides

Environment variables take precedence over config.ini:

```bash
# Global log level
export DPC_LOG_LEVEL=DEBUG

# Console output
export DPC_LOG_CONSOLE=true

# Custom log file
export DPC_LOG_FILE=/tmp/dpc-debug.log

# Per-module levels
export DPC_LOG_MODULE_LEVELS="dpc_client_core.service:DEBUG,dpc_client_core.hub_client:INFO"
```

## Log Levels

**DEBUG** - Detailed diagnostic information
- Connection states, message flow, protocol details
- Use for troubleshooting specific issues
- Example: `logger.debug("Received HELLO from %s", node_id)`

**INFO** - General informational messages
- Service startup/shutdown, connection events, state changes
- Default level for production
- Example: `logger.info("Connected to peer %s via WebRTC", node_id)`

**WARNING** - Warning messages (non-critical issues)
- Retryable errors, deprecated features, potential problems
- Example: `logger.warning("Hub connection lost, will retry in %d seconds", retry_delay)`

**ERROR** - Error messages (critical issues)
- Exceptions, failed operations, unrecoverable errors
- Always includes stack traces with `exc_info=True`
- Example: `logger.error("Failed to parse message: %s", e, exc_info=True)`

**CRITICAL** - Critical system failures
- Reserved for catastrophic failures requiring immediate attention
- Example: `logger.critical("Database corrupted, shutting down")`

## Per-Module Configuration

Enable detailed logging for specific modules:

```ini
[logging]
log_level = INFO
module_levels = dpc_client_core.webrtc_peer:DEBUG,dpc_client_core.p2p_manager:DEBUG
```

**Common modules:**
- `dpc_client_core.service` - Main service orchestration
- `dpc_client_core.p2p_manager` - P2P connection management
- `dpc_client_core.webrtc_peer` - WebRTC connections
- `dpc_client_core.hub_client` - Hub communication
- `dpc_client_core.llm_manager` - AI provider integration
- `dpc_client_core.local_api` - UI WebSocket API
- `dpc_protocol.crypto` - Cryptographic operations
- `dpc_protocol.protocol` - Message serialization

## Development vs Production

### Development Setup

```ini
[logging]
log_level = DEBUG
console_output = true
module_levels = dpc_client_core.webrtc_peer:DEBUG
```

**Behavior:**
- All logs output to console (stdout/stderr)
- All logs written to file
- Detailed DEBUG messages for troubleshooting

### Production Setup

```ini
[logging]
log_level = INFO
console_output = false
```

**Behavior:**
- Only INFO and above written to file
- No console output (run as daemon/service)
- Minimal performance overhead

## Troubleshooting Common Issues

### WebRTC Connection Problems

```bash
export DPC_LOG_MODULE_LEVELS="dpc_client_core.webrtc_peer:DEBUG,dpc_client_core.p2p_manager:DEBUG"
poetry run python run_service.py
```

Look for:
- ICE candidate gathering failures
- STUN/TURN server connectivity
- Certificate validation errors

### Hub Authentication Issues

```bash
export DPC_LOG_MODULE_LEVELS="dpc_client_core.hub_client:DEBUG"
poetry run python run_service.py
```

Look for:
- OAuth callback errors
- JWT token validation failures
- WebSocket signaling problems

### AI Query Failures

```bash
export DPC_LOG_MODULE_LEVELS="dpc_client_core.llm_manager:DEBUG,dpc_client_core.service:DEBUG"
poetry run python run_service.py
```

Look for:
- Provider connection errors
- Context window overflow
- Model not found errors

### Message Protocol Errors

```bash
export DPC_LOG_MODULE_LEVELS="dpc_protocol.protocol:DEBUG"
poetry run python run_service.py
```

Look for:
- Message framing errors
- JSON serialization failures
- Command validation errors

## Viewing Logs

### Real-time Monitoring (Linux/macOS)

```bash
tail -f ~/.dpc/logs/dpc-client.log
```

### Real-time Monitoring (Windows)

```powershell
Get-Content ~\.dpc\logs\dpc-client.log -Wait -Tail 50
```

### Filter by Level

```bash
# Show only errors
grep "ERROR" ~/.dpc/logs/dpc-client.log

# Show errors and warnings
grep -E "ERROR|WARNING" ~/.dpc/logs/dpc-client.log
```

### Filter by Module

```bash
# Show all WebRTC peer logs
grep "dpc_client_core.webrtc_peer" ~/.dpc/logs/dpc-client.log

# Show connection-related logs
grep -E "p2p_manager|webrtc_peer" ~/.dpc/logs/dpc-client.log
```

## Log Rotation

Logs automatically rotate when they reach 10MB:

```
~/.dpc/logs/
├── dpc-client.log          # Current log
├── dpc-client.log.1        # Most recent backup
├── dpc-client.log.2        # Second most recent
├── dpc-client.log.3
├── dpc-client.log.4
└── dpc-client.log.5        # Oldest backup (deleted on next rotation)
```

**Manual cleanup:**

```bash
# Remove all old logs
rm ~/.dpc/logs/dpc-client.log.*

# Remove logs older than 7 days (Linux/macOS)
find ~/.dpc/logs -name "dpc-client.log.*" -mtime +7 -delete
```

## Best Practices

### For Developers

1. **Use appropriate log levels:**
   - DEBUG for detailed flow tracking
   - INFO for state changes and events
   - WARNING for retryable errors
   - ERROR for failures with `exc_info=True`

2. **Use % formatting (not f-strings):**
   ```python
   # Good (lazy evaluation, better performance)
   logger.debug("Processing message from %s", node_id)

   # Bad (evaluates even if DEBUG disabled)
   logger.debug(f"Processing message from {node_id}")
   ```

3. **Include context in error logs:**
   ```python
   try:
       process_message(msg)
   except Exception as e:
       logger.error("Failed to process message from %s: %s", node_id, e, exc_info=True)
   ```

4. **Don't log sensitive data:**
   ```python
   # Bad - logs private keys
   logger.debug("Signing with key: %s", private_key)

   # Good - logs key fingerprint
   logger.debug("Signing with key: %s...", key_fingerprint[:16])
   ```

### For Users

1. **Start with INFO level** - Most useful for general troubleshooting
2. **Enable DEBUG only when needed** - Generates large log files
3. **Use per-module configuration** - Target specific components
4. **Monitor log file size** - Rotation helps, but DEBUG can grow fast

## Integration with Other Tools

### systemd Journal (Linux)

Capture logs in systemd journal:

```ini
[Unit]
Description=D-PC Messenger Client
After=network.target

[Service]
Type=simple
User=dpc
WorkingDirectory=/opt/dpc-messenger/dpc-client/core
ExecStart=/opt/dpc-messenger/dpc-client/core/.venv/bin/python run_service.py
StandardOutput=journal
StandardError=journal
Restart=always

[Install]
WantedBy=multi-user.target
```

View logs:
```bash
journalctl -u dpc-messenger -f
```

### Docker

Mount log directory as volume:

```yaml
version: '3'
services:
  dpc-client:
    image: dpc-messenger-client
    volumes:
      - ./logs:/root/.dpc/logs
    environment:
      - DPC_LOG_LEVEL=INFO
      - DPC_LOG_CONSOLE=false
```

View logs:
```bash
docker logs -f dpc-client
```

## Migration Notes

**Pre-v0.8.0:** Used `print()` statements throughout codebase

**Post-v0.8.0:** Uses Python standard library `logging`

**Changes:**
- All emojis removed from log output
- Consistent formatting across all modules
- File-based logging with rotation
- Per-module log level configuration
- Better integration with system logging tools

**Backwards Compatibility:**
- CLI tools (cli_backup.py) still use `print()` for user interaction
- Demo/test sections in files preserved with `print()` for readability
