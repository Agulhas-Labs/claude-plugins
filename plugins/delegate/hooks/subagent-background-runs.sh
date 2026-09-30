#!/bin/sh
# SubagentStop: run subagent-background-runs.py through the Python finder. The hook command names this
# script alone, by a literal ${CLAUDE_PLUGIN_ROOT} path, as the plugin directory's validator requires.
exec sh "${CLAUDE_PLUGIN_ROOT}/hooks/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/hooks/subagent-background-runs.py"
