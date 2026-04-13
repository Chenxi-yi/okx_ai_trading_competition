#!/bin/bash
# Start or attach to the quant-trading Claude Code session
SESSION="quant-trading"
PROJECT="/Users/lucaslee/quant_trade_competition/engine"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Attaching to existing session: $SESSION"
    tmux attach-session -t "$SESSION"
else
    echo "Creating new session: $SESSION"
    tmux new-session -d -s "$SESSION" -c "$PROJECT"
    tmux send-keys -t "$SESSION" "cd $PROJECT && claude" Enter
    tmux attach-session -t "$SESSION"
fi
