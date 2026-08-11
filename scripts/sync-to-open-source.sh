#!/bin/bash
# Sync local cursor-telegram-bridge to open-source repository (AmazingDraw/cursor-telegram-bridge).

set -e

MSG="${1:-update open-source release}"
PROJECT_NAME="cursor-telegram-bridge"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${HOME}/Projects/GitHub Copilot/${PROJECT_NAME}"

echo "==> Syncing ${PROJECT_NAME} -> ${DEST_DIR}"
mkdir -p "${DEST_DIR}"

# 1. Copy tracked git files (exclude private-only / non-OSS paths)
cd "${SRC_DIR}"
git ls-files | while read -r file; do
    case "${file}" in
        tests|tests/*)
            continue
            ;;
    esac
    dir="$(dirname "${DEST_DIR}/${file}")"
    mkdir -p "${dir}"
    cp "${SRC_DIR}/${file}" "${DEST_DIR}/${file}"
done

# Drop excluded trees left over from earlier syncs
rm -rf "${DEST_DIR}/tests"

# 2. Sanitization pass
cd "${DEST_DIR}"
find . -type f \( -name "*.md" -o -name "*.py" -o -name "*.toml" -o -name "*.sh" -o -name "*.json" -o -name "*.example" \) | while read -r file; do
    # Replace absolute path references
    sed -i '' "s|~/|~/|g" "${file}" 2>/dev/null || true
    # Replace GitHub user repo URLs
    sed -i '' "s|AmazingDraw/cursor-telegram-bridge|AmazingDraw/cursor-telegram-bridge|g" "${file}" 2>/dev/null || true
    # Replace personal numeric Telegram IDs
    sed -i '' "s|123456789|123456789|g" "${file}" 2>/dev/null || true
done

# Ensure rules.md in open source edition uses clean example template if rules.md.example exists
if [ -f "rules.md.example" ]; then
    cp rules.md.example rules.md
fi

echo "==> Sanitization complete."

# 3. Git status in destination
if [ ! -d ".git" ]; then
    git init
    git remote add origin "https://github.com/AmazingDraw/${PROJECT_NAME}.git" 2>/dev/null || true
fi

echo "==> Open-source directory status:"
git status --short

if [ "${SKIP_CONFIRM}" != "1" ]; then
    read -p "Proceed with git add, commit and push to AmazingDraw/${PROJECT_NAME}? (y/N) " confirm
    if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
        echo "Sync finished (push cancelled by user)."
        exit 0
    fi
fi

git add -A
if git diff --staged --quiet; then
    echo "No changes to commit in open-source repository."
else
    git commit -m "${MSG}"
    git push -u origin main || git push -u origin master
    echo "✅ Successfully synced and pushed to AmazingDraw/${PROJECT_NAME}"
fi
