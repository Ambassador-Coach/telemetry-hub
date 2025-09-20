#!/bin/bash

# Color-coded wrapper scripts for different AI agents
# Makes each agent's output visually distinct

# ANSI Color Codes
CLAUDE_COLOR="\033[1;36m"     # Cyan
GEMINI_COLOR="\033[1;32m"     # Green  
CODEX_COLOR="\033[1;35m"      # Magenta
GPT_COLOR="\033[1;33m"         # Yellow
RESET="\033[0m"

# Claude wrapper (Cyan)
claude-colored() {
    echo -e "${CLAUDE_COLOR}[CLAUDE]${RESET} Starting Claude session..."
    # Prepend color to each line of output
    claude "$@" | while IFS= read -r line; do
        echo -e "${CLAUDE_COLOR}[CLAUDE]${RESET} $line"
    done
}

# Gemini wrapper (Green)
gemini-colored() {
    echo -e "${GEMINI_COLOR}[GEMINI]${RESET} Starting Gemini session..."
    gemini "$@" | while IFS= read -r line; do
        echo -e "${GEMINI_COLOR}[GEMINI]${RESET} $line"
    done
}

# Codex wrapper (Magenta) 
codex-colored() {
    echo -e "${CODEX_COLOR}[CODEX]${RESET} Starting Codex session..."
    codex "$@" | while IFS= read -r line; do
        echo -e "${CODEX_COLOR}[CODEX]${RESET} $line"
    done
}

# GPT wrapper (Yellow)
gpt-colored() {
    echo -e "${GPT_COLOR}[GPT]${RESET} Starting GPT session..."
    # Replace with actual GPT CLI command
    gpt "$@" | while IFS= read -r line; do
        echo -e "${GPT_COLOR}[GPT]${RESET} $line"
    done
}

# Interactive menu
show_menu() {
    echo -e "\n${CLAUDE_COLOR}1)${RESET} Claude (Cyan)"
    echo -e "${GEMINI_COLOR}2)${RESET} Gemini (Green)"
    echo -e "${CODEX_COLOR}3)${RESET} Codex (Magenta)"
    echo -e "${GPT_COLOR}4)${RESET} GPT (Yellow)"
    echo -e "5) Exit\n"
    read -p "Select agent: " choice
    
    case $choice in
        1) claude-colored ;;
        2) gemini-colored ;;
        3) codex-colored ;;
        4) gpt-colored ;;
        5) exit 0 ;;
        *) echo "Invalid option" && show_menu ;;
    esac
}

# If script is run directly, show menu
if [ "$#" -eq 0 ]; then
    show_menu
else
    # Run specific agent if provided as argument
    case $1 in
        claude) shift && claude-colored "$@" ;;
        gemini) shift && gemini-colored "$@" ;;
        codex) shift && codex-colored "$@" ;;
        gpt) shift && gpt-colored "$@" ;;
        *) echo "Usage: $0 [claude|gemini|codex|gpt] [args...]" ;;
    esac
fi