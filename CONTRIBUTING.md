# Contributing

Thanks for helping out. This tool patches a **minified, closed-source bundle**
that Anthropic re-releases almost daily, so contributions are a little unusual.
The most common and most valuable one is **re-anchoring a patch that stopped
matching after an extension update.**

## How the patcher works

`patch-extension.py` finds the installed Claude Code extension and rewrites
files inside it — `webview/index.css`, `webview/index.js`, and `extension.js`.

Every individual patch is:

- **Idempotent and self-detecting** — it looks for its own already-applied form
  (a marker string) before doing anything, so re-running is always safe.
- **Regex-anchored on stable strings** — the bundle is minified and the short
  identifiers change every release, so a patch anchors on stable string
  literals and *wildcards the identifiers around them* (`\w{1,3}` and friends).
  This is what lets a patch survive re-minification.
- **Backed up** — the patcher writes a timestamped `.bak` beside any file it
  changes, before changing it.

Each patch is a function with this shape:

```python
def patch_example(text: str) -> tuple[str, list[str], list[str]]:
    """One-line description of what it fixes."""
    applied: list[str] = []
    skipped: list[str] = []
    if MARKER in text:                      # already patched — bail
        return text, [], ["example (already)"]
    m = SOME_REGEX.search(text)
    if not m:                               # bundle changed — skip cleanly
        return text, [], ["example (pattern not found — re-anchor needed)"]
    new_text = ...                          # do the edit
    applied.append("example (what changed)")
    return new_text, applied, skipped
```

It is then registered in `patch_css`, `patch_js`, or `patch_extension_js`.

## A patch broke after an update — how to fix it

When Anthropic ships a release with a re-minified bundle, a patch's regex can
stop matching. The patcher reports it clearly:

```
js SKIP (example (pattern not found — re-anchor needed)): index.js
```

It does **not** corrupt the file — it just skips that patch. To fix it:

1. Open the relevant file in the installed extension directory:
   - `~/.vscode-server/extensions/anthropic.claude-code-*/` (Remote / SSH)
   - `~/.vscode/extensions/anthropic.claude-code-*/` (local install)
2. Find the code the patch targets. The patch's comment block usually quotes
   the shape it expects.
3. Update the regex / string constants in `patch-extension.py` so they anchor
   on the new minified form. Keep identifiers wildcarded — do not hard-code the
   new short names, they will change again next release.
4. Test (below), bump `build.txt`, and open a PR.

## Testing

```sh
# Apply against your installed extension
python3 patch-extension.py

# Confirm the patched bundle is still valid JavaScript
node --check ~/.vscode-server/extensions/anthropic.claude-code-*/extension.js
node --check ~/.vscode-server/extensions/anthropic.claude-code-*/webview/index.js
```

A patch must leave the file as valid JS even when it fails to match — a clean
`SKIP` is correct behavior, a syntax error is not. Reload VS Code
(`Developer: Reload Window`) to see a change live.

To start from a clean bundle, restore from the `.bak` files the patcher wrote,
or reinstall the extension.

## Conventions

- `build.txt` is a plain integer; bump it on every commit.
- One logical change per commit.
- A new patch must be idempotent, self-detecting, and back up cleanly.
- Keep patches generic — anything tied to a personal setup does not belong here.

## Reporting a broken patch

If you do not want to fix it yourself, open an issue with the **"Patch broke
after an update"** template and paste the patcher's `SKIP` output plus your
extension version. That is genuinely useful — it flags the drift for everyone.
