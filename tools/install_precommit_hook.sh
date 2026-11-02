#!/bin/sh
# Install the local pre-commit hook that refuses data files.
#
# Run this from inside the checkout to protect:  sh tools/install_precommit_hook.sh
#
# The hook refuses a staged *.npz, *.jsonl, *.db* or audio file anywhere in the
# tree. Those files hold voiceprints, meeting history or recordings. Removing
# such a file from git stays allowed. `git commit --no-verify` skips the hook.
#
# Git keeps one hooks folder for a repository and all its worktrees, so this
# protects every worktree. The script refuses to replace a hook it did not write.
set -eu

MARKER="stt-workflow data-file guard"
hooks_dir=$(git rev-parse --git-path hooks)
target="$hooks_dir/pre-commit"

if [ -e "$target" ] && ! grep -q "$MARKER" "$target"; then
    echo "A different pre-commit hook exists at $target. Nothing changed." >&2
    exit 1
fi

mkdir -p "$hooks_dir"
tmp="$target.tmp.$$"
cat > "$tmp" <<'HOOK'
#!/bin/sh
# stt-workflow data-file guard. Installed by tools/install_precommit_hook.sh.
bad=$(git diff --cached --name-only --diff-filter=ACMRT -z \
    | tr '\0' '\n' \
    | grep -iE '(\.npz|\.jsonl|\.db[^/]*|\.(m4a|m4b|wav|mp3|aif|aiff|aac|caf|flac|ogg|opus|amr|mp4|mov|webm))$' \
    || true)
if [ -n "$bad" ]; then
    echo "Commit refused. These staged files can hold private data:" >&2
    printf '%s\n' "$bad" | sed 's/^/  /' >&2
    echo "Unstage them with: git restore --staged <path>" >&2
    exit 1
fi
exit 0
HOOK
chmod 755 "$tmp"
mv "$tmp" "$target"
echo "Installed $target"
