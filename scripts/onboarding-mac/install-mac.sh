#!/bin/bash

# DPC Messenger - One-Command Installer for macOS
# For non-technical users
# This script will install all dependencies and set up DPC Messenger

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Print banner
print_banner() {
    echo -e "${BLUE}"
    cat << "EOF"
╔═══════════════════════════════════════════════════════╗
║                                                       ║
║         DPC Messenger - Installer for Mac             ║
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

# Check if running on macOS
check_os() {
    print_step "Checking operating system"
    
    if [[ $(uname) != "Darwin" ]]; then
        print_error "This script is designed for macOS only"
        print_info "Current OS: $(uname)"
        exit 1
    fi
    
    print_success "macOS detected"
    
    # Get macOS version
    MACOS_VERSION=$(sw_vers -productVersion)
    print_info "macOS version: $MACOS_VERSION"
}

# Check and install Homebrew
check_homebrew() {
    print_step "Checking Homebrew"
    
    if ! command -v brew &> /dev/null; then
        print_warning "Homebrew not found. Installing..."
        
        # Ask for confirmation
        echo -e "${YELLOW}Homebrew is required to install dependencies.${NC}"
        read -p "Install Homebrew now? (y/n) " -n 1 -r
        echo
        
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            
            # Add brew to PATH for current session
            if [[ $(uname -m) == "arm64" ]]; then
                export PATH="/opt/homebrew/bin:$PATH"
                print_info "Apple Silicon detected"
            else
                export PATH="/usr/local/bin:$PATH"
                print_info "Intel Mac detected"
            fi
            
            print_success "Homebrew installed"
        else
            print_error "Homebrew is required. Installation cancelled."
            exit 1
        fi
    else
        print_success "Homebrew installed"
        brew_version=$(brew --version | head -n 1)
        print_info "Version: $brew_version"
    fi
}

# Check and install Python 3.12
check_python() {
    print_step "Checking Python 3.12"
    
    if command -v python3.12 &> /dev/null; then
        PYTHON_VERSION=$(python3.12 --version)
        print_success "Python 3.12 found: $PYTHON_VERSION"
    else
        print_warning "Python 3.12 not found. Installing..."
        brew install python@3.12
        print_success "Python 3.12 installed"
    fi
}

# Check and install Poetry
check_poetry() {
    print_step "Checking Poetry"
    
    if command -v poetry &> /dev/null; then
        POETRY_VERSION=$(poetry --version)
        print_success "Poetry found: $POETRY_VERSION"
    else
        print_warning "Poetry not found. Installing..."
        curl -sSL https://install.python-poetry.org | python3 -
        
        # Add poetry to PATH for current session
        export PATH="$HOME/.local/bin:$PATH"
        
        print_success "Poetry installed"
    fi
}

# Check and install Node.js
check_nodejs() {
    print_step "Checking Node.js"
    
    if command -v node &> /dev/null; then
        NODE_VERSION=$(node --version)
        NPM_VERSION=$(npm --version)
        print_success "Node.js found: $NODE_VERSION"
        print_info "npm version: $NPM_VERSION"
    else
        print_warning "Node.js not found. Installing..."
        brew install node
        print_success "Node.js installed"
    fi
}

# Clone or update repository
setup_repository() {
    print_step "Setting up DPC Messenger"
    
    DPC_DIR="$HOME/dpc-messenger"
    
    if [ -d "$DPC_DIR" ]; then
        print_warning "Directory already exists: $DPC_DIR"
        read -p "Update existing installation? (y/n) " -n 1 -r
        echo
        
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            cd "$DPC_DIR"
            git pull origin main
            print_success "Repository updated"
        else
            print_info "Using existing installation"
        fi
    else
        print_info "Cloning repository..."
        git clone https://github.com/mikhashev/dpc-messenger.git "$DPC_DIR"
        print_success "Repository cloned"
    fi
    
    cd "$DPC_DIR"
}

# Install Python dependencies
install_python_deps() {
    print_step "Installing Python dependencies"

    cd "$DPC_DIR/dpc-client/core"

    print_info "This may take a few minutes..."
    poetry install

    print_success "Python dependencies installed"
}

# Install frontend dependencies
install_frontend_deps() {
    print_step "Installing frontend dependencies"

    cd "$DPC_DIR/dpc-client/ui"

    print_info "This may take a few minutes..."
    npm install

    print_success "Frontend dependencies installed"

    cd "$DPC_DIR"
}

# Create desktop shortcut
create_shortcut() {
    print_step "Creating desktop shortcut"
    
    DPC_DIR="$HOME/dpc-messenger"
    SCRIPT_DIR="$DPC_DIR/scripts"
    
    # Ensure scripts directory exists
    mkdir -p "$SCRIPT_DIR"
    
    # Create launch script if it doesn't exist
    if [ ! -f "$SCRIPT_DIR/launch-mac.sh" ]; then
        print_warning "Launch script not found. Please ensure launch-mac.sh exists."
        return
    fi
    
    # Make launch script executable
    chmod +x "$SCRIPT_DIR/launch-mac.sh"
    
    # Create desktop shortcut
    DESKTOP_DIR="$HOME/Desktop"
    
    if [ -d "$DESKTOP_DIR" ]; then
        cat > "$DESKTOP_DIR/DPC Messenger.command" << EOF
#!/bin/bash
cd "$DPC_DIR"
"$SCRIPT_DIR/launch-mac.sh"
EOF
        chmod +x "$DESKTOP_DIR/DPC Messenger.command"
        print_success "Desktop shortcut created"
        print_info "Double-click 'DPC Messenger' on your desktop to start"
    else
        print_warning "Desktop directory not found"
    fi
}

# Create configuration directory
setup_config() {
    print_step "Setting up configuration"
    
    DPC_CONFIG="$HOME/.dpc"
    
    if [ ! -d "$DPC_CONFIG" ]; then
        mkdir -p "$DPC_CONFIG"
        print_success "Configuration directory created: $DPC_CONFIG"
    else
        print_success "Configuration directory exists: $DPC_CONFIG"
    fi
}

# Print completion message
print_completion() {
    echo -e "\n${GREEN}"
    cat << "EOF"
╔═══════════════════════════════════════════════════════╗
║                                                       ║
║              ✅ Installation Complete!                ║
║                                                       ║
╚═══════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
    
    echo -e "${CYAN}How to start DPC Messenger:${NC}\n"
    
    echo -e "${YELLOW}Option 1: Desktop shortcut${NC}"
    echo -e "  Double-click ${GREEN}'DPC Messenger'${NC} on your desktop\n"
    
    echo -e "${YELLOW}Option 2: Terminal${NC}"
    echo -e "  Run: ${GREEN}~/dpc-messenger/scripts/launch-mac.sh${NC}\n"
    
    echo -e "${CYAN}What's next?${NC}"
    echo -e "  1. Read the quickstart guide: ${GREEN}~/dpc-messenger/docs/MAC_QUICKSTART.md${NC}"
    echo -e "  2. Open DPC Messenger and create your profile"
    echo -e "  3. Start collaborating with AI!\n"
    
    echo -e "${CYAN}Need help?${NC}"
    echo -e "  Documentation: ${BLUE}https://github.com/mikhashev/dpc-messenger${NC}"
    echo -e "  Issues: ${BLUE}https://github.com/mikhashev/dpc-messenger/issues${NC}\n"
}

# Main installation flow
main() {
    print_banner
    
    print_info "Starting installation process..."
    print_info "This will install: Homebrew, Python 3.12, Poetry, Node.js"
    echo
    
    read -p "Continue? (y/n) " -n 1 -r
    echo
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_warning "Installation cancelled"
        exit 0
    fi
    
    check_os
    check_homebrew
    check_python
    check_poetry
    check_nodejs
    setup_repository
    install_python_deps
    install_frontend_deps
    create_shortcut
    setup_config
    
    print_completion
}

# Run main function
main
