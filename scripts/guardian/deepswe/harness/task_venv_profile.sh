# Preserve the task image's declared virtual environment in login shells.
#
# Codex executes repository commands through ``bash -lc``. Some base images'
# /etc/profile replaces Docker's configured PATH, silently selecting the system
# interpreter instead of the task environment. Keep this arm-blind: the
# ablation launcher mounts the same profile fragment for solo and Guardian.
if [ -n "${VIRTUAL_ENV:-}" ] && [ -d "${VIRTUAL_ENV}/bin" ]; then
    PATH="${VIRTUAL_ENV}/bin:${PATH}"
    export PATH
fi
