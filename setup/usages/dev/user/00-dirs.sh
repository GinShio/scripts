#!/usr/bin/env bash
#@tags: usage:dev, scope:user
# User: Directories

echo "Creating directory structure..."
mkdir -p "$HOME/Projects"
mkdir -p "$HOME/.local/"{bin,share,lib64}
mkdir -p "$HOME/.local/share/"{fonts,applications}
