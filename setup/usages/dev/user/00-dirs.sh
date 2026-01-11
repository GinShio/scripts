#!/usr/bin/env bash
# User: Directories

echo "Creating directory structure..."
mkdir -p "$HOME/Projects"
mkdir -p "$HOME/.local/"{bin,share,lib64}
mkdir -p "$HOME/.local/share/"{fonts,applications}
