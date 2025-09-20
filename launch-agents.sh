#!/bin/bash

# Launch multiple terminals with different color schemes for each agent
# Works with gnome-terminal, konsole, or xterm

# Function to launch colored terminal
launch_terminal() {
    local title="$1"
    local bg_color="$2"
    local fg_color="$3"
    local command="$4"
    
    # Check which terminal emulator is available
    if command -v gnome-terminal &> /dev/null; then
        gnome-terminal --title="$title" \
            --profile-preferences-background-color="$bg_color" \
            --profile-preferences-foreground-color="$fg_color" \
            -- bash -c "$command; exec bash"
    elif command -v konsole &> /dev/null; then
        konsole --title "$title" \
            --profile "Agent-$title" \
            -e bash -c "$command; exec bash"
    elif command -v xterm &> /dev/null; then
        xterm -title "$title" \
            -bg "$bg_color" \
            -fg "$fg_color" \
            -e bash -c "$command; exec bash" &
    else
        echo "No supported terminal emulator found"
    fi
}

# Launch different agents with unique colors
echo "Launching color-coded agent terminals..."

# Claude - Blue theme
launch_terminal "Claude" "#001133" "#00ccff" "claude"

sleep 0.5

# Gemini - Green theme
launch_terminal "Gemini" "#001100" "#00ff00" "gemini" 

sleep 0.5

# Codex - Purple theme
launch_terminal "Codex" "#220033" "#cc00ff" "codex"

sleep 0.5

# GPT - Orange theme  
launch_terminal "GPT" "#331100" "#ff9900" "gpt"

echo "All agent terminals launched!"