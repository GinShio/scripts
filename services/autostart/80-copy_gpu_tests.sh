#!/usr/bin/env bash

source $XDG_CONFIG_HOME/workflow/.env
python3 $PROJECTS_SCRIPT_DIR/gputest.py install
python3 $PROJECTS_SCRIPT_DIR/gputest.py restore --days 10
