#!/bin/bash

# DPC Messenger - Unified Launcher for macOS
# Automatically starts backend and frontend in one terminal window
# For non-technical users

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DPC_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_LOG="$DPC_DIR/logs/backend.log"
FRONTEND_LOG="$DPC_DIR/logs/frontend.log"
PID_FILE="$DPC_DIR/logs/backend.pid"

# Print banner
print_banner() {
    clear
    echo -e "${BLUE}"
    cat << "EOF"
╔═══════════════════════════════════════════════════════╗
║                                                       ║
║              DPC Messenger v0.20.0                    ║
║                                                       ║
║   Privacy-First Infrastructure for                   ║
║   Human-AI-Team Collaboration                        ║
║                                                       ║
╚═══════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
}

# Print step header
print_step() {
    echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

# Print success message
print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

# Print error message
print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Print warning message
print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

# Print info message
print_info() {
    echo -e "${CYAN}ℹ️  $1${NC}"
}

# Check if DPC directory exists
check_dpc_directory() {
    if [ ! -d "$DPC_DIR" ]; then
        print_error "DPC Messenger directory not found: $DPC_DIR"
        print_info "Please run the installer first: ~/dpc-messenger/scripts/install-mac.sh"
        exit 1
    fi
    
    cd "$DPC_DIR"
    print_success "Found DPC Messenger directory"
}

# Check dependencies
check_dependencies() {
    print_step "Checking dependencies"
    
    local missing_deps=()
    
    # Check Poetry
    if ! command -v poetry &> /dev/null; then
        missing_deps+=("Poetry")
    else
        print_success "Poetry found"
    fi
    
    # Check Python
    if ! command -v python3 &> /dev/null; then
        missing_deps+=("Python 3")
    else
        print_success "Python found"
    fi
    
    # Check Node.js
    if ! command -v node &> /dev/null; then
        missing_deps+=("Node.js")
    else
        print_success "Node.js found"
    fi
    
    # Check if dependencies are installed
    if [ ${#missing_deps[@]} -gt 0 ]; then
        print_error "Missing dependencies: ${missing_deps[*]}"
        print_info "Please run the installer first: ~/dpc-messenger/scripts/install-mac.sh"
        exit 1
    fi
    
    # Check if virtual environment exists
    if [ ! -d "$DPC_DIR/.venv" ]; then
        print_warning "Virtual environment not found"
        print_info "Installing dependencies..."
        poetry install
        cd "$DPC_DIR/dpc-client/ui"
        poetry run npm install
        cd "$DPC_DIR"
        print_success "Dependencies installed"
    fi
}

# Create logs directory
setup_logs() {
    mkdir -p "$DPC_DIR/logs"
    print_success "Logs directory ready"
}

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Stopping DPC Messenger..."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${NC}"
    
    # Kill backend process
    if [ -f "$PID_FILE" ]; then
        BACKEND_PID=$(cat "$PID_FILE")
        if ps -p $BACKEND_PID > /dev/null 2>&1; then
            kill $BACKEND_PID 2>/dev/null || true
            print_success "Backend stopped (PID: $BACKEND_PID)"
        fi
        rm -f "$PID_FILE"
    fi
    
    # Also try to kill by process name
    pkill -f "run_service.py" 2>/dev/null || true
    
    echo -e "${GREEN}✅ DPC Messenger stopped${NC}\n"
    
    # Show log locations
    echo -e "${CYAN}Logs saved to:${NC}"
    echo -e "  Backend: $BACKEND_LOG"
    echo -e "  Frontend: $FRONTEND_LOG\n"
    
    exit 0
}

# Set up signal handlers
trap cleanup SIGINT SIGTERM

# Start backend
start_backend() {
    print_step "Starting backend"
    
    cd "$DPC_DIR"
    
    # Check if already running
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if ps -p $OLD_PID > /dev/null 2>&1; then
            print_warning "Backend already running (PID: $OLD_PID)"
            print_info "Stopping old instance..."
            kill $OLD_PID 2>/dev/null || true
            sleep 2
        fi
    fi
    
    # Start backend in background
    print_info "Starting backend service..."
    (cd "$DPC_DIR/dpc-client/core" && poetry run python run_service.py) > "$BACKEND_LOG" 2>&1 &
    BACKEND_PID=$!
    
    # Save PID
    echo $BACKEND_PID > "$PID_FILE"
    
    print_success "Backend started (PID: $BACKEND_PID)"
    
    # Wait for backend to be ready
    print_info "Waiting for backend to initialize..."
    
    local max_attempts=30
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        # Check if process is still running
        if ! ps -p $BACKEND_PID > /dev/null 2>&1; then
            print_error "Backend failed to start"
            echo -e "\n${RED}Backend log:${NC}"
            tail -n 20 "$BACKEND_LOG"
            exit 1
        fi
        
        # Check if backend is responding (look for specific log message)
        if grep -q "D-PC Core Service started" "$BACKEND_LOG" 2>/dev/null; then
            print_success "Backend is ready!"
            return 0
        fi
        
        sleep 1
        attempt=$((attempt + 1))
        echo -n "."
    done
    
    echo
    print_warning "Backend startup taking longer than expected"
    print_info "Check logs: $BACKEND_LOG"
}

# Start frontend
start_frontend() {
    print_step "Starting frontend"
    
    cd "$DPC_DIR/dpc-client/ui"

    print_info "Starting web interface..."
    print_info "DPC Messenger will open in your browser"
    echo

    # Start frontend (this will block)
    npm run dev 2>&1 | tee "$FRONTEND_LOG"
}

# Show running info
show_running_info() {
    echo -e "\n${GREEN}"
    cat << "EOF"
╔═══════════════════════════════════════════════════════╗
║                                                       ║
║              🚀 DPC Messenger Running                ║
║                                                       ║
╚═══════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
    
    echo -e "${CYAN}Backend:${NC} Running (PID: $BACKEND_PID)"
    echo -e "${CYAN}Frontend:${NC} Starting..."
    echo -e "${CYAN}Logs:${NC} $DPC_DIR/logs"
    echo
    echo -e "${YELLOW}Press Ctrl+C to stop${NC}\n"
}

# Main function
main() {
    print_banner
    
    check_dpc_directory
    check_dependencies
    setup_logs
    start_backend
    show_running_info
    start_frontend
}

# Run main function
main "$@"
