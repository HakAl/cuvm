#!/bin/zsh
# Runs over pinned SSH on a disposable stock Tart macOS clone.
set -eu
setopt PIPE_FAIL

export PATH="/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1
export npm_config_prefix=/opt/homebrew
export HTTPS_PROXY="$1" HTTP_PROXY="$1" https_proxy="$1" http_proxy="$1"
export NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1"
unset NODE_EXTRA_CA_CERTS
codex_version="$2"
mcp_version="$3"
webkit_revision="$4"

sudo -n true
sudo -n pmset -a displaysleep 0 sleep 0 disksleep 0
printf 'admin ALL=(ALL) NOPASSWD: ALL\n' | sudo -n tee /etc/sudoers.d/cuvm >/dev/null
sudo -n chmod 440 /etc/sudoers.d/cuvm
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' |
    sudo -n tee /etc/ssh/sshd_config.d/000-cuvm.conf >/dev/null
sudo -n /usr/sbin/sshd -t
sudo -n /usr/sbin/sshd -T | grep -qx 'passwordauthentication no'
sudo -n /usr/sbin/sshd -T | grep -qx 'kbdinteractiveauthentication no'
command -v node >/dev/null
command -v npm >/dev/null

if ! command -v tmux >/dev/null; then
    brew install tmux >&2
fi
codex_integrity=$(npm view --registry=https://registry.npmjs.org "@openai/codex@$codex_version" dist.integrity)
mcp_integrity=$(npm view --registry=https://registry.npmjs.org "@playwright/mcp@$mcp_version" dist.integrity)
npm install --global --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org \
    "@openai/codex@$codex_version" "@playwright/mcp@$mcp_version" >&2

module_root=$(npm root -g)
test "$module_root" = /opt/homebrew/lib/node_modules
playwright="$module_root/@playwright/mcp/node_modules/.bin/playwright"
"$playwright" install webkit >&2
test "$(codex --version)" = "codex-cli $codex_version"

CODEX_INTEGRITY="$codex_integrity" MCP_INTEGRITY="$mcp_integrity" \
    node - "$module_root" "$codex_version" "$mcp_version" "$webkit_revision" "$(tmux -V)" <<'JS'
const fs = require('node:fs');
const crypto = require('node:crypto');
const [modules, codexVersion, mcpVersion, webkitRevision, tmuxVersion] = process.argv.slice(2);
const codex = modules + '/@openai/codex';
const mcp = modules + '/@playwright/mcp';
const binary = codex + '/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex';
const entry = mcp + '/cli.js';
const { webkit } = require(mcp + '/node_modules/playwright');
const sha256 = path => crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex');

async function main() {
    if (require(codex + '/package.json').version !== codexVersion ||
        require(mcp + '/package.json').version !== mcpVersion ||
        !webkit.executablePath().split('/').includes(webkitRevision)) {
        throw new Error('installed dependencies differ from the build pins');
    }
    if (!process.env.CODEX_INTEGRITY || !process.env.MCP_INTEGRITY) {
        throw new Error('npm did not report package integrity metadata');
    }
    fs.accessSync(binary, fs.constants.X_OK);
    fs.accessSync(webkit.executablePath(), fs.constants.X_OK);
    const browser = await webkit.launch();
    try {
        const page = await browser.newPage();
        await page.setContent('<title>cuvm prepare</title>');
        if (await page.title() !== 'cuvm prepare') throw new Error('WebKit launch check failed');
    } finally {
        await browser.close();
    }
    console.log(JSON.stringify({
        codex: {
            bin: '/opt/homebrew/bin/codex', version: codexVersion,
            binary, binary_sha256: sha256(binary)
        },
        browser: {
            node: process.execPath, node_version: process.version, playwright_mcp: mcpVersion,
            entry, entry_sha256: sha256(entry), engines: { webkit: webkitRevision }
        },
        packages: {
            '@openai/codex': process.env.CODEX_INTEGRITY,
            '@playwright/mcp': process.env.MCP_INTEGRITY,
            tmux: tmuxVersion
        }
    }));
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
JS
