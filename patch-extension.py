#!/usr/bin/env python3
"""
Patch the bundled webview of the Claude Code VS Code extension.

Three changes, all idempotent:

  1. CSS — enlarge the attachment pill ONLY when it contains an image
     thumbnail. Text/document context pills (build.txt, settings.json, etc)
     stay at the stock 24px height. Achieved with a :has(.thumbIcon) selector
     so the geometry only applies when the pill has a thumbnail child. The
     thumbnail image itself is also enlarged via the .thumbIcon rule.

  2. CSS — make the pill remove (X) button always visible, not only on hover.
     The button and its onRemove handler already exist in the bundle; the
     stock CSS hides it behind opacity:0 until you hover the pill.

  3. JS — gate the click-to-open behavior on tool body rows (the IN and OUT
     blocks in the chat). Plain clicks no longer open the tool output as an
     editor tab. Cmd+click (or Ctrl+click) still opens it. The cursor pointer
     affordance is removed so the row looks like plain text.

Migration: an earlier shape of this script (utils Build 1-4, this repo
Builds 1-2) replaced the .pill_lcdCYQ rule directly, which made every pill
80px tall — including text-context pills. On bundles where that old form
is detected, the patcher reverts the over-broad rules to original before
applying the new scoped rule.

Searches for installed extension folders in:
  ~/.vscode-server/extensions/anthropic.claude-code-*   (Remote-SSH server)
  ~/.vscode/extensions/anthropic.claude-code-*          (local install)

Backs up modified files to <name>.bak.<timestamp> before editing.
"""

from __future__ import annotations
import glob
import os
import re
import shutil
import sys
import time
from pathlib import Path

# --- Tunable image-pill sizes (only applied to pills with a thumbnail image) -
PILL_HEIGHT = 80          # original 24 (stock pill height, kept for non-image)
THUMB_SIZE = 72           # original 12
PILL_MAX_WIDTH = 300      # original 180
PILL_GAP = 7              # original 4

# --- Internal --------------------------------------------------------------

EXTENSION_GLOBS = [
    "~/.vscode-server/extensions/anthropic.claude-code-*",
    "~/.vscode/extensions/anthropic.claude-code-*",
]

# Original CSS rules — must match the bundle byte-for-byte.
ORIGINAL_PILL_TAIL = (
    "gap:4px;min-width:0;max-width:180px;height:24px;"
    "transition:border-color .15s}"
)
ORIGINAL_THUMB_RULE = (
    ".thumbIcon_lcdCYQ{object-fit:cover;border-radius:max(1px,"
    "calc(var(--pill-radius) - var(--pill-padding)));flex-shrink:0;"
    "width:12px;height:12px}"
)
ORIGINAL_LABEL_RULE = (
    ".label_lcdCYQ{color:var(--app-primary-foreground);white-space:nowrap;"
    "overflow:hidden;text-overflow:ellipsis;min-width:0;font-size:11px;"
    "font-weight:500}"
)
ORIGINAL_REMOVE_BUTTON_RULE = (
    ".removeButton_lcdCYQ{position:absolute;display:flex;"
    "border-radius:0 var(--pill-radius)var(--pill-radius)0;"
    "cursor:pointer;opacity:0;background:linear-gradient(to right,"
    "transparent 0%,var(--pill-bg)50%,var(--pill-bg)100%);border:none;"
    "justify-content:flex-end;align-items: center;width:32px;"
    "padding:0 6px 0 0;transition:opacity .15s;top:0;bottom:0;right:0}"
)

# Old over-broad shapes the patcher used to write (utils Build 1-4, this
# repo Build 1-2). When detected, the new patcher reverts them so text-context
# pills stop rendering at image-pill size.
OLD_PATCHED_PILL_TAIL = (
    "gap:7px !important;min-width:0;max-width:300px !important;"
    "height:80px !important;min-height:80px !important;"
    "transition:border-color .15s}"
)
OLD_PATCHED_LABEL_RULE = (
    ".label_lcdCYQ{color:var(--app-primary-foreground);white-space:nowrap;"
    "overflow:hidden;text-overflow:ellipsis;min-width:0;"
    "font-size:13px !important;font-weight:500}"
)

# New scoped image-pill rule. Appended to the CSS so the cascade picks it
# only when a .pill has a .thumbIcon child (image kind).
IMAGE_PILL_RULE_MARKER = ".pill_lcdCYQ:has(.thumbIcon_lcdCYQ){"

REMOVE_BUTTON_PATCHED_MARKER = (
    "cursor:pointer;opacity:1 !important;background:linear-gradient(to right,"
)

# JS patterns: only tool body rows (className:J0.toolBodyRowContent) get the
# click-gating. The unrelated "secondaryLine" match is skipped.
#
# Two render-path variants exist in the bundle:
#   1. Conditional cursor — `style:VAR?{cursor:"pointer"}:void 0`. Used when
#      the row may or may not have a click handler (e.g. tool input/output
#      where the openable-as-tab decision is dynamic).
#   2. Unconditional cursor — `style:{cursor:"pointer"}`. Used by the slash
#      command / prompt input row, which is always clickable.
#
# Both have to be patched or you can still trigger an unwanted open by
# clicking on the slash command IN row.

JS_TOOL_ROW_COND_PATTERN = re.compile(
    r'(className:J0\.toolBodyRowContent,)'
    r'onClick:([A-Za-z_$][A-Za-z0-9_$]*),'
    r'style:\2\?\{cursor:"pointer"\}:void 0'
)
JS_TOOL_ROW_UNCOND_PATTERN = re.compile(
    r'(className:J0\.toolBodyRowContent,)'
    r'onClick:([A-Za-z_$][A-Za-z0-9_$]*),'
    r'style:\{cursor:"pointer"\}(?!:)'
)
# Bare ()=>{...openContent(...)} arrow functions — used by Glob/Grep summary
# rows, the Agent input, and a few other tools. Catches the click handler
# wherever it appears, including inside inline ternaries.
JS_ARROW_VOID_OPENCONTENT_PATTERN = re.compile(
    r'\(\)=>\{(\$\.fileOpener\.openContent\([^)]+\))\}'
)
# REPL header click — already takes the event arg as Q, just adds the gate.
JS_REPL_PATTERN = re.compile(
    r'\(Q\)=>\{Q\.stopPropagation\(\),Q\.preventDefault\(\),'
    r'(\$\.fileOpener\.openContent\([^)]+\))\}'
)
# Diagnostics J() function — declared without args, called from {onClick:J}.
# Replace the function declaration to take an event arg and gate.
JS_DIAGNOSTICS_PATTERN = re.compile(
    r'function J\(\)\{let Y=RN0\(\$\);'
    r'Z\.fileOpener\.openContent\(Y,"Diagnostics: VSCode Problems",!1\)\}'
)

JS_PATCHED_MARKER = "if(e.metaKey||e.ctrlKey)"

# --- Dynamic nav-tab label width ------------------------------------------
# The stock navTabLabel is hard-capped at max-width:120px regardless of how
# many tabs are open or how wide the sidebar is. On an ultrawide monitor with
# three sessions open, every label gets a "Investigate Session Reap…" ellipsis
# even though there is hundreds of pixels of unused header room.
#
# Earlier builds tiered the cap by tab count (1-2 → 400px, 3-4 → 75px, etc.).
# That was still hardcoded — same 75px on a 14" MBP and a 5K ultrawide.
#
# Build 37+ — fully dynamic via flex layout. Tabs share whatever container
# width is available; ellipsis kicks in only when content actually overflows.
#   - .navTab_hONcXw becomes a flex container with `flex:1 1 0` so siblings
#     split the available room equally and `min-width:0` so flex-shrink can
#     clamp them below content size.
#   - .navTabLabel_hONcXw drops the fixed max-width (set to none) and uses
#     `flex:1 1 0; min-width:0` so the label fills its pill but truncates
#     with the existing overflow:hidden + text-overflow:ellipsis when the
#     pill is squeezed.
#   - 5+/7+ tab font-size shrinks are kept — they pack more characters per
#     pixel when the sidebar is dense, independent of any width cap.
ORIGINAL_NAV_TAB_LABEL_RULE = (
    ".navTabLabel_hONcXw{white-space:nowrap;overflow:hidden;"
    "text-overflow:ellipsis;max-width:120px}"
)
DYNAMIC_TAB_WIDTH_MARKER = ".navTab_hONcXw{display:flex;align-items:center;flex:1 1 0"

# Old form Build 7-15 — no 1-2 tab base override, only 3+/5+/7+ shrinks.
OLD_DYNAMIC_TAB_WIDTH_RULES_V1 = (
    "*:has(>.navTab_hONcXw:nth-child(3)) .navTabLabel_hONcXw{max-width:75px}"
    "*:has(>.navTab_hONcXw:nth-child(5)) .navTabLabel_hONcXw{max-width:50px}"
    "*:has(>.navTab_hONcXw:nth-child(7)) .navTabLabel_hONcXw{max-width:35px}"
)
# Old form Build 16 — added 1-2 tab base override but still single-line at 5+/7+.
OLD_DYNAMIC_TAB_WIDTH_RULES_V2 = (
    ".navTabLabel_hONcXw{max-width:400px}"
    "*:has(>.navTab_hONcXw:nth-child(3)) .navTabLabel_hONcXw{max-width:75px}"
    "*:has(>.navTab_hONcXw:nth-child(5)) .navTabLabel_hONcXw{max-width:50px}"
    "*:has(>.navTab_hONcXw:nth-child(7)) .navTabLabel_hONcXw{max-width:35px}"
)
# Old form Build 17 — 2-line 5+/7+ with hardcoded 75px at 3-4 tabs.
OLD_DYNAMIC_TAB_WIDTH_RULES_V3 = (
    ".navTabLabel_hONcXw{max-width:400px}"
    "*:has(>.navTab_hONcXw:nth-child(3)) .navTabLabel_hONcXw{max-width:75px}"
    "*:has(>.navTab_hONcXw:nth-child(5)) .navTab_hONcXw{font-size:.7em}"
    "*:has(>.navTab_hONcXw:nth-child(5)) .navTabLabel_hONcXw"
    "{max-width:90px;white-space:normal;display:-webkit-box;"
    "-webkit-box-orient:vertical;-webkit-line-clamp:2;line-height:1.15}"
    "*:has(>.navTab_hONcXw:nth-child(7)) .navTab_hONcXw{font-size:.65em}"
    "*:has(>.navTab_hONcXw:nth-child(7)) .navTabLabel_hONcXw{max-width:70px}"
)

DYNAMIC_TAB_WIDTH_RULES = (
    ".navTab_hONcXw{display:flex;align-items:center;flex:1 1 0;min-width:0}"
    ".navTabLabel_hONcXw{flex:1 1 0;min-width:0;max-width:none}"
    "*:has(>.navTab_hONcXw:nth-child(5)) .navTab_hONcXw{font-size:.7em}"
    "*:has(>.navTab_hONcXw:nth-child(7)) .navTab_hONcXw{font-size:.65em}"
)

# --- Active-session title bar — uncap width --------------------------------
# Stock: titleGroup is capped at max-width:300px so even short auto-generated
# titles like "Extend title length for repository descriptions" truncate with
# ellipsis when there is plenty of horizontal room in the header.
# Patch: drop just the max-width so the title bar grows to fit its content
# up to the available header width. The headerSpacer (flex:1) still keeps
# any right-side buttons pinned right. Single-line ellipsis behavior in
# titleTextInner is left intact for the rare title that exceeds the header
# width — better than wrapping the header onto two rows.
ORIGINAL_TITLE_GROUP_RULE = (
    ".titleGroup_aqhumA{display:flex;overflow:hidden;"
    "font-size:var(--vscode-chat-font-size,13px);"
    "font-family:var(--vscode-chat-font-family);"
    "border-radius:6px;align-items:stretch;min-width:0;max-width:300px}"
)
PATCHED_TITLE_GROUP_RULE = (
    ".titleGroup_aqhumA{display:flex;overflow:hidden;"
    "font-size:var(--vscode-chat-font-size,13px);"
    "font-family:var(--vscode-chat-font-family);"
    "border-radius:6px;align-items:stretch;min-width:0}"
)
TITLE_UNLOCK_PATCHED_MARKER = (
    "border-radius:6px;align-items:stretch;min-width:0}"
)

# --- Image-only submit ----------------------------------------------------
# The chat input has TWO gates that block submission when the trimmed text
# content is empty. To allow submitting a paste-only image, patch both:
#
#   1. The Enter key handler: `if(z8(),t.current?.textContent?.trim()||"")k1()`
#      currently only calls the submit (k1) when the trimmed text is truthy.
#      Patched form: always call k1.
#
#   2. The submit function k1 itself: `let I1=t.current?.textContent?.trim()||
#      "";if(!I1)return;` — bails if I1 is empty.
#      Patched form: bail when text is empty AND no attachment chip is in the
#      DOM. The .thumbIcon_lcdCYQ class on the attachment pill is the DOM
#      signal that an image is attached and pending send. Also, if I1 is empty
#      AFTER the gate (image-only path), substitute U+200B (ZERO WIDTH SPACE)
#      so the message body is never an empty string. Without this substitution,
#      the CLI agent builds an empty text content block with cache_control set
#      on it, and the Anthropic API rejects with "cache_control cannot be set
#      for empty text blocks". ZWS is non-empty to the API but renders as
#      nothing in the chat UI, so no visible placeholder character appears in
#      the conversation. JS String.prototype.trim() does not strip ZWS, so it
#      survives any downstream re-trim. Build 14 used "." instead, which
#      worked but was visible in the chat. ZWS landed in Build 17. Diagnosed
#      2026-05-03.
#
# DOM-based attachment check is hacky but avoids reverse-engineering the
# component's internal React state names, which change on every minified
# rebuild.
ORIGINAL_ENTER_GATE = (
    'if(z8(),t.current?.textContent?.trim()||"")k1()'
)
PATCHED_ENTER_GATE = (
    'if(z8(),1)k1()'
)
ORIGINAL_K1_GATE = (
    'let I1=t.current?.textContent?.trim()||"";if(!I1)return;'
)
# Old broken form (Build 4-13) — image-only worked but sent "" downstream → API 400.
OLD_PATCHED_K1_GATE = (
    'let I1=t.current?.textContent?.trim()||"";'
    'if(!I1&&!document.querySelector(".thumbIcon_lcdCYQ"))return;'
)
# Build 14-16 form — visible "." placeholder. Worked but ugly.
BUILD14_PATCHED_K1_GATE = (
    'let I1=t.current?.textContent?.trim()||"";'
    'if(!I1&&!document.querySelector(".thumbIcon_lcdCYQ"))return;'
    'if(!I1)I1=".";'
)
# New form — invisible ZWS (U+200B) placeholder.
PATCHED_K1_GATE = (
    'let I1=t.current?.textContent?.trim()||"";'
    'if(!I1&&!document.querySelector(".thumbIcon_lcdCYQ"))return;'
    'if(!I1)I1="​";'
)
IMAGE_ONLY_PATCHED_MARKER = 'if(!I1)I1="​";'

# --- Live override-aware title in the in-session title bar ------------------
# Stock: the title-bar JSX renders `$.activeSession.value?.summary.value`
# directly. session.summary is a Signal that gets set ONCE when the session
# loads from the JSONL transcript. So when an external writer (the namer
# system in vscode-session-namer) appends a custom-title event to the
# transcript or pushes an entry into ~/.claude/session-overrides.json, the
# in-webview title bar stays stale until the next session-resume.
#
# The bundle already has an override-aware reader: `Wk(session)` checks
# `globalThis.__cceOverridesSig` (which extension.js polls from
# session-overrides.json every 1 second and posts to the webview) before
# falling back to `session.summary.value`. The OS-level editor tab title
# uses Wk and updates live; the in-webview title bar does not.
#
# Patch: replace every `$.activeSession.value?.summary.value||"Untitled"`
# in the title-bar JSX (4 sites — rename-mode capture, input defaultValue,
# button tooltip, visible span) with a Wk-aware ternary that handles the
# null-active-session case Wk itself does not guard.
ORIGINAL_TITLE_SUMMARY_READ = (
    '$.activeSession.value?.summary.value||"Untitled"'
)
# The title-func identifier (Wk/Kk/etc.) drifts with every Anthropic bundle
# re-minify. Both the live-title ternary and the wk-override body need to
# refer to whatever the current name is, and we need to *rewrite* old
# patched ternaries when the name changes underneath us — otherwise the
# ternary ends up calling a stale identifier that the new bundle assigned
# to something else entirely (e.g. the React import alias).
RE_TITLE_FUNC = re.compile(
    r'function ([A-Za-z_$][A-Za-z_$0-9]*)\(\$\)\{return \$\.summary\.value\|\|"Untitled"\}'
)
RE_PATCHED_TITLE_TERNARY = re.compile(
    r'\(\$\.activeSession\.value\?([A-Za-z_$][A-Za-z_$0-9]*)\(\$\.activeSession\.value\):"Untitled"\)'
)
WK_OVERRIDE_PATCHED_MARKER = '_sig=_g.__cceOverridesSig'


# --- Restore the override pipeline (regression in 2.1.128) ------------------
# 2.1.126 had three pieces working together:
#   1. extension.js polled ~/.claude/session-overrides.json every second and
#      posted {type:"cce_override_update",overrides:_o} into each webview.
#   2. webview/index.js had a one-shot listener that wrote those overrides
#      onto globalThis.__cceOverridesSig.value.
#   3. webview/index.js had Wk(session) check that signal before falling back
#      to session.summary.value.
# 2.1.128 stripped all three. Wk became `function Wk($){return $.summary.value||"Untitled"}`,
# the listener and the polling loop are gone. Our existing live-title and
# force-title-bar patches still apply (they call Wk), but Wk is now a stub —
# which is why the namer system writes new titles to disk and the in-webview
# bar never updates. These three patches restore the pipeline by lifting the
# 2.1.126 source verbatim, with one tweak: __cceOverridesSig is initialized
# as a plain {value:null} object instead of a preact-signal so the patch does
# not depend on a minified Signal constructor name.
# Signal factory regex — matches `activeSession=O0(void 0)` to capture the
# minified signal-factory name. Falling back to a plain {value:null} object
# leaves __cceOverridesSig non-reactive, so Wk re-reads its value but Preact
# never re-renders the title-bar JSX. With a real signal, the listener's
# `_sig.value=d.overrides` write triggers reactivity across all three displays
# (editor tab, sidebar, in-webview title bar) on the same 1s polling tick.
RE_SIGNAL_FACTORY = re.compile(r'\bactiveSession=([A-Za-z_$][A-Za-z_$0-9]*)\(void 0\)')

OVERRIDES_CONSUMER_PATCHED_MARKER = '__cceListenerInstalled'

# Build 25-37 form: postMessage only — updated only the in-webview title bar.
# The editor tab (panel.title) was set ONCE at session-load by stock parsing
# of JSONL custom-title events and never refreshed at runtime. Long sessions
# would drift: the namer rewrote session-overrides.json on every drift-check,
# the in-webview bar tracked it live, but the editor tab stayed pinned to
# whatever title was active when the panel first opened. Documented in the
# CLAUDE.md "four display contexts" table as if it worked, but it didn't.
OLD_OVERRIDES_PRODUCER_IIFE_V1 = (
    '(function(){try{'
    'var _fs=require("fs"),_pa=require("path"),'
    '_op=_pa.join(require("os").homedir(),".claude","session-overrides.json");'
    'var _send=function(){try{'
    'var _o={};'
    'try{_o=JSON.parse(_fs.readFileSync(_op,"utf8"))}catch(e){}'
    'V.webview.postMessage({type:"cce_override_update",overrides:_o})'
    '}catch(e){}};'
    'setInterval(_send,1000);_send()'
    '}catch(e){}})()'
)

# Build 38+ form: also writes V.title=_o[K] so the editor tab actually
# tracks the override.
#
# Why K is the sessionId here:
#   The IIFE is injected after `this.webviews.add(<name>)`. There are 3
#   call sites in the bundle:
#     - resolveWebviewView(V, K, B)   — sidebar webview
#     - setupPanel(V, K, B, x)        — session panel ← K = sessionId
#     - resolveSessionListView(V, K, B) — session list view
#
#   Confirmed by tracing the only setupPanel caller in the bundle:
#     `if(this.setupPanel(U, V, K, q), V) this.sessionPanels.set(V, U)`
#   The outer V is the sessionId stored as the Map key. Inside setupPanel
#   it is renamed to K (param 2). For the other two sites, K is not a
#   sessionId, so `_o[K]` is undefined and the V.title= write no-ops safely.
#
# Why all three try/catches:
#   resolveSessionListView's V is a WebviewView, not a WebviewPanel.
#   WebviewView.title is settable per VS Code API but defensive try/catch
#   protects against any host where it isn't.
#
# What will break this patch in a future Anthropic release:
#   - Renaming the K parameter in setupPanel (e.g. K → N). The patcher's
#     re-anchor logic is the same as folder-search/session-open-column —
#     update OVERRIDES_PRODUCER_IIFE to reference the new param name.
#   - Removing this.sessionPanels.set(V, U) — would break sessionId capture
#     entirely. Check archive/<version>-darwin-arm64.tar.gz to diff.
# 2.1.140 form: setupPanel/resolveWebviewView/resolveSessionListView were
# refactored — the panel is now the FIRST param `z`, and `V` is something
# else (a column number / token / boolean). Writing `V.title=_o[K]` from
# inside these functions targeted the wrong identifier and silently no-op'd
# inside the try/catch. The editor tab stayed pinned to the load-time
# aiTitle. Fix: reference `z` (the panel) instead. Kept as
# BROKEN_V_OVERRIDES_PRODUCER_IIFE so installs still carrying the dead form
# get migrated to the working z-form.
BROKEN_V_OVERRIDES_PRODUCER_IIFE = (
    '(function(){try{'
    'var _fs=require("fs"),_pa=require("path"),'
    '_op=_pa.join(require("os").homedir(),".claude","session-overrides.json");'
    'var _send=function(){try{'
    'var _o={};'
    'try{_o=JSON.parse(_fs.readFileSync(_op,"utf8"))}catch(e){}'
    'try{V.webview.postMessage({type:"cce_override_update",overrides:_o})}catch(e){}'
    'try{if(typeof K==="string"&&_o[K]&&V.title!==_o[K])V.title=_o[K]}catch(e){}'
    '}catch(e){}};'
    'setInterval(_send,1000);_send()'
    '}catch(e){}})()'
)
# Build 55 form: writes z.title=_o[K] only when the function parameter K is a
# string sessionId. That assumption holds for the `createPanel` call site
# (this.setupPanel(B, z, K, Z) → formal K = the sessionId) but FAILS for the
# `registerWebviewPanelSerializer.deserializeWebviewPanel` call site:
#     D.setupPanel(w, void 0, void 0, I)
# When VS Code restores a previously-opened tab on window reload, this is the
# path it uses. K is undefined, the override-write no-ops forever, and the
# editor tab stays stuck at the load-time aiTitle even though the in-session
# title bar (which gets the full overrides object via postMessage) updates
# correctly. Diagnosed 2026-05-18 on session 3d0750d5 in 2.1.143.
# Kept as BUILD_55_OVERRIDES_PRODUCER_IIFE so installs carrying it migrate.
BUILD_55_OVERRIDES_PRODUCER_IIFE = (
    '(function(){try{'
    'var _fs=require("fs"),_pa=require("path"),'
    '_op=_pa.join(require("os").homedir(),".claude","session-overrides.json");'
    'var _send=function(){try{'
    'var _o={};'
    'try{_o=JSON.parse(_fs.readFileSync(_op,"utf8"))}catch(e){}'
    'try{z.webview.postMessage({type:"cce_override_update",overrides:_o})}catch(e){}'
    'try{if(typeof K==="string"&&_o[K]&&z.title!==_o[K])z.title=_o[K]}catch(e){}'
    '}catch(e){}};'
    'setInterval(_send,1000);_send()'
    '}catch(e){}})()'
)

# Build 56 form: captures the enclosing class instance via .call(this) and,
# when K is not a string sessionId, reverse-looks-up the sessionId by
# scanning this.sessionPanels for the panel reference. Handles the
# deserializeWebviewPanel(w, void 0, void 0, I) path correctly: once the
# session attaches (and sessionPanels.set(D, z) fires), the next 1-second
# tick resolves the sessionId and writes the title override.
OVERRIDES_PRODUCER_IIFE = (
    '(function(){try{'
    'var _self=this;'
    'var _fs=require("fs"),_pa=require("path"),'
    '_op=_pa.join(require("os").homedir(),".claude","session-overrides.json");'
    'var _send=function(){try{'
    'var _o={};'
    'try{_o=JSON.parse(_fs.readFileSync(_op,"utf8"))}catch(e){}'
    'try{z.webview.postMessage({type:"cce_override_update",overrides:_o})}catch(e){}'
    'try{'
    'var _sid=(typeof K==="string")?K:null;'
    'if(!_sid&&_self&&_self.sessionPanels&&_self.sessionPanels.forEach)'
    '_self.sessionPanels.forEach(function(_v,_k){if(_v===z)_sid=_k});'
    'if(_sid&&_o[_sid]&&z.title!==_o[_sid])z.title=_o[_sid];'
    '}catch(e){}'
    '}catch(e){}};'
    'setInterval(_send,1000);_send()'
    '}catch(e){}}).call(this)'
)
# Marker is a substring unique to the Build 56 form (reverse-lookup branch).
# Bundles carrying the Build 55 z-form will NOT match this marker and will be
# migrated.
OVERRIDES_PRODUCER_PATCHED_MARKER = '_self.sessionPanels.forEach'
RE_WEBVIEWS_ADD = re.compile(r'(this\.webviews\.add\([A-Za-z0-9_\$]+\))([,;])')

# --- Force in-session title bar in full-editor mode -------------------------
# Stock: the title bar (titleGroup + rename pencil + headerSpacer) is wrapped
# in `!window.IS_FULL_EDITOR && createElement(Fragment, ...)`. Sessions opened
# via claude-vscode.primaryEditor.open pass ViewColumn.Active, which the host
# detects (N=B===ViewColumn.Active) and forwards to the webview as
# IS_FULL_EDITOR=true — so those sessions never get the in-webview title bar.
# The VS Code editor tab title above is then the only place to see or rename
# the session, and it truncates aggressively when many tabs are open.
#
# Patch: remove just the first IS_FULL_EDITOR guard so the title bar always
# renders, regardless of how the panel was opened. The second
# !window.IS_FULL_EDITOR guard later in the same JSX wraps an onboarding
# fragment and is left alone.
#
# Anchored on `{className:h6.header},` so the patch only matches the header
# children list, not any other IS_FULL_EDITOR check elsewhere in the bundle.
ORIGINAL_TITLE_GUARD = (
    '{className:h6.header},'
    '!window.IS_FULL_EDITOR&&'
    'p0.default.createElement(p0.default.Fragment,null,'
    'p0.default.createElement("div",{ref:J,className:`${h6.titleGroup}'
)
PATCHED_TITLE_GUARD = (
    '{className:h6.header},'
    'p0.default.createElement(p0.default.Fragment,null,'
    'p0.default.createElement("div",{ref:J,className:`${h6.titleGroup}'
)
TITLE_GUARD_PATCHED_MARKER = PATCHED_TITLE_GUARD

# --- Folder @mention selection -----------------------------------------------
# When you click a directory in the @ mention dropdown the extension currently
# just populates the text input with the folder path so you can keep typing to
# drill into it — it does NOT add a context chip. Patching `m1` to always be
# true makes directories behave identically to files: a chip is created and the
# cursor moves past a trailing space so the mention is closed.
# Regex form — captures the minified helper-function name (`Gq1` in early
# bundles, `Qq1` as of 2.1.136) so a single-character rename does not break
# the anchor. The rest of the literal string is stable across recent builds.
DIR_MENTION_RE = re.compile(
    r'let m1=!\(I1&&d\.type==="directory"\),_0=m1\?d\.path\+" ":d\.path,'
    r'k0=(?P<helper>[A-Za-z_$][\w$]*)\(D,L2,_0,!0\);'
    r'if\(t\.current\.textContent=k0,A\(k0\),m1\)'
    r'o1\(\(O5\)=>new Set\(O5\)\.add\(`@\$\{d\.path\}`\)\);'
    r'let l0=t\.current\.firstChild\|\|t\.current;if\(l0\)\{'
    r'let O5=document\.createRange\(\),S2=m1\?1:0,'
)
DIR_MENTION_PATCHED_MARKER = 'let m1=!0,_0=d.path+" "'

# --- User message arrival timestamp ----------------------------------------
# Stamp each new user message bubble with arrival time, James's local time.
# The chat UI does not display user message timestamps natively. Replaces the
# prior approach where Claude echoed `[Your msg: ...]` in its response.
#
# Only NEW user messages get stamped. Existing ones loaded from session history
# at webview start are left blank — their original send time is not knowable
# from the DOM.
#
# V4/V5 (Build 34): move the stamp from a block under the bubble to an
# inline chip prefixing the message text. James asked for this on
# 2026-05-07 ("inject the date with some formatting inline with my text
# just before it"). Two changes:
#   1. The JS IIFE now stamps the inner `userMessage_HASH` element (the
#      bubble itself) instead of the outer `userMessageContainer_HASH`. This
#      lets ::before sit inline with the bubble text rather than as a block
#      child of the wrapper.
#   2. The CSS selector switches to `[data-arrived]::before` (inline).
# V5 (same-session follow-up): keep seconds in the format string. James
# asked for them back right after seeing V4 without them. Format is now
# "May 6, 2026 3:12:47 PM" — same precision as V3, just inline now.
# Stamping the inner bubble also naturally avoids the duplicate-stamp bug
# that V2/V3 dodged with a previous-sibling check: only the main container
# has a `userMessage_` child; the attachments-wrapper container does not.
USER_MSG_TIME_CSS_RULE = (
    '[data-arrived]::before{'
    'content:attr(data-arrived) "  ";'
    'font-size:10px;'
    'font-family:var(--vscode-editor-font-family,monospace);'
    'color:var(--app-secondary-foreground);'
    'opacity:.55;'
    'margin-right:6px;'
    'letter-spacing:.02em;'
    'user-select:none;'
    'white-space:pre'
    '}'
)
USER_MSG_TIME_CSS_MARKER = '[data-arrived]::before'
# Old V1–V3 CSS form — block ::after under the container. Cleanup pass
# strips this from any live bundle that still has it, so machines pick up
# the inline V4 form without waiting for the next Anthropic release.
USER_MSG_TIME_CSS_OLD_MARKER = '[class*="userMessageContainer_"]::after'

USER_MSG_TIME_JS_IIFE = (
    "\n;(function(){"
    "if(globalThis.__cceUserMsgStampInstalledV5)return;"
    "globalThis.__cceUserMsgStampInstalledV5=true;"
    "function _f(d){"
    "var mo=['January','February','March','April','May','June','July','August','September','October','November','December'],"
    "h=d.getHours(),m=d.getMinutes(),s=d.getSeconds(),"
    "ap=h>=12?'PM':'AM',h12=h%12||12;"
    "return mo[d.getMonth()]+' '+d.getDate()+', '+d.getFullYear()+' '+h12+':'+('0'+m).slice(-2)+':'+('0'+s).slice(-2)+' '+ap"
    "}"
    "function _stamp(c){"
    "if(c.hasAttribute('data-cce-stamped'))return;"
    "c.setAttribute('data-cce-stamped','1');"
    "var inner=c.querySelector('[class*=\"userMessage_\"]');"
    "if(!inner)return;"
    "if(inner.hasAttribute('data-arrived'))return;"
    "inner.setAttribute('data-arrived',_f(new Date()))"
    "}"
    "new MutationObserver(function(mu){"
    "for(var i=0;i<mu.length;i++){"
    "var an=mu[i].addedNodes;"
    "for(var j=0;j<an.length;j++){"
    "var n=an[j];"
    "if(n.nodeType!==1)continue;"
    "if(n.matches&&n.matches('[class*=\"userMessageContainer_\"]'))_stamp(n);"
    "if(n.querySelectorAll){"
    "var ds=n.querySelectorAll('[class*=\"userMessageContainer_\"]');"
    "for(var k=0;k<ds.length;k++)_stamp(ds[k])"
    "}}}"
    "}).observe(document.body,{childList:true,subtree:true})"
    "})();\n"
)
USER_MSG_TIME_JS_MARKER = "__cceUserMsgStampInstalledV5"
# Old markers — the cleanup pass strips V1 (raw milliseconds, double-
# stamping), V2 (raw milliseconds, no double-stamping), V3 (block ::after
# with seconds), and V4 (inline, no seconds). V5 is inline with seconds.
USER_MSG_TIME_JS_MARKERS_OLD = (
    "__cceUserMsgStampInstalled=true",
    "__cceUserMsgStampInstalledV2=true",
    "__cceUserMsgStampInstalledV3=true",
    "__cceUserMsgStampInstalledV4=true",
)

# --- Gold star on the active Local/Web tab ----------------------------------
# Build 28 originally placed this star on `.sessionItem_OOQiHg.active_OOQiHg`
# (the selected session row). Build 31 moved it to the active tab in the
# Local/Web segmented control above the session list, which is the actual
# "where am I" indicator James wants.
#
# Historical context: this was first wired up by editing the live JSX on
# 2026-05-05 ("Local ★" / "Web ★" via inline span), but never made it into the
# patcher, so it got lost on the next Anthropic release when the watcher
# re-patched. Build 31 makes it permanent here as a CSS-only patch — no JSX
# substitution to chase across minifier renames.
#
# JSX shape (from 2.1.132 webview/index.js):
#   <div class="segmented_HASH">
#     <button class="tab_HASH tabActive_HASH"><Pe1/>Local</button>
#     <button class="tab_HASH">             <Bs/>Web</button>
#   </div>
#
# Class-prefix selector ([class*="segmented_"] etc.) survives the per-release
# hash bump. ::after places the star at the end of the active tab — after the
# "Local" or "Web" label — matching the historical "Local ★" placement.
ACTIVE_TAB_STAR_CSS = (
    '[class*="segmented_"] [class*="tab_"][class*="tabActive_"]::after{'
    'content:" \\2605";'
    'color:#FFD700;'
    'font-size:13px;'
    'flex-shrink:0;'
    'line-height:1;'
    'margin-left:4px;'
    'text-shadow:0 0 3px rgba(255,215,0,.5)'
    '}'
)
ACTIVE_TAB_STAR_MARKER = '[class*="segmented_"] [class*="tab_"][class*="tabActive_"]::after'
# Old Build 28 selector — used by the cleanup pass to strip the session-row
# star CSS rule left behind in the live bundle on machines that ran Build 28.
OLD_ACTIVE_SESSION_STAR_PREFIX = ".sessionItem_OOQiHg.active_OOQiHg::before"

# --- Show parent folders in tool-call rows ----------------------------------
# Anthropic's webview renders Read / Edit / Write / MultiEdit tool-call rows
# by displaying ONLY the basename of the touched file: every callsite calls
# `Z.file_path.split("/").pop()` and uses the result as the link text. With
# many parallel sessions across multiple repos, "patch-extension.py" alone is
# ambiguous — James needs to see which repo owns the file at a glance, per
# his fully-qualified-paths memory.
#
# Historical context: this was patched into the live bundle on 2026-05-03
# (see /tmp/patch_studio_server_extension.py + patch_mbp_extension_v2.py in
# the JSONLs from that session) but never made it into patch-extension.py.
# Every Anthropic re-emit + watcher re-patch since then has reverted it.
# Build 33 makes it permanent.
#
# Format: `.slice(-3).join("/")` — last 3 segments, e.g.
# `vscode-extension/patch-extension.py` or
# `cc/memory/feedback_fully_qualified_paths.md`. Enough to ID the repo,
# without the noise of `/Users/ojhurst/apps/`.
#
# Seven sites across the 2.1.132 webview bundle. Click handlers untouched —
# they always open the full path stored on the tool input object.
FULL_PATH_TOOL_HEADER_PATCHES = (
    # site 1 — RA.fileToolHeader (Edit / Write / MultiEdit reuse this)
    (
        'fileToolHeader($,Z,J){let Y=Z?.split("/").pop();',
        'fileToolHeader($,Z,J){let Y=Z?.split("/").slice(-3).join("/");',
    ),
    # site 2 — Read class header override
    (
        'header($,Z){let J=Z.file_path?.split("/").pop();',
        'header($,Z){let J=Z.file_path?.split("/").slice(-3).join("/");',
    ),
    # site 3 — Read fileReads array (batched / coalesced read display)
    (
        'let G=J.file_path.split("/").pop()||J.file_path;',
        'let G=J.file_path.split("/").slice(-3).join("/")||J.file_path;',
    ),
    # site 4 — ExitPlanMode plan-file path
    (
        'let Y=Z.planFilePath.split("/").pop(),',
        'let Y=Z.planFilePath.split("/").slice(-3).join("/"),',
    ),
    # site 5 — NotebookEdit header
    (
        'let J=Z.notebook_path?.split("/").pop();',
        'let J=Z.notebook_path?.split("/").slice(-3).join("/");',
    ),
    # site 6 — Edit permissionRequest dialog
    (
        'permissionRequest($,Z){let J=Z.file_path.split("/").pop();',
        'permissionRequest($,Z){let J=Z.file_path.split("/").slice(-3).join("/");',
    ),
    # site 7 — Read / Write permissionRequest dialog (uses ||"" fallback form)
    (
        'permissionRequest($,Z){let J=(Z.file_path||"").split("/").pop();',
        'permissionRequest($,Z){let J=(Z.file_path||"").split("/").slice(-3).join("/");',
    ),
)
FULL_PATH_TOOL_HEADER_MARKER = 'fileToolHeader($,Z,J){let Y=Z?.split("/").slice(-3).join("/");'

# --- Folder search by name in @ mention dropdown -----------------------------
# The IW() function in extension.js builds its folder list solely from parent
# paths of files whose FILENAME matches the query. Typing "@vscode-exten"
# finds screenshot PNGs (which have that string in their name) but never
# surfaces the vscode-extension/ folder because its children (build.txt, etc.)
# do not contain that string in their filename.
#
# Fix: after the existing file-derived directory pass, run a second findFiles
# with "**/*<query>*/**" to find any file inside a folder whose name matches.
# Extract the matching folder segments and add them as directory results.
#
# Anthropic re-minifies every release and identifiers rotate constantly. The
# previous approach maintained one literal-string ORIGINAL/PATCHED pair per
# rotation (V1 through V5) which broke on every minor release. The current
# approach matches the dir-sort fragment with a single regex that uses named
# backreferences, so the seven rotating single-letter idents (lowercased
# query, raw query, outer iterator, folder Set, result array, sort param a,
# sort param b) self-heal. The vscode module name and exclusion options var
# are pulled from the surrounding function with a separate lookback regex.
# Structural changes (e.g. the 2.1.138 regression from type-priority sort to
# pure-alphabetical) still need a human; identifier rotations do not.
IW_DIR_SORT_RE = re.compile(
    r'let\s+(?P<lc>\w{1,3})=(?P<q>\w{1,3})\?\.toLowerCase\(\);'
    r'for\(let\s+(?P<it>\w{1,3})\s+of\s+(?P<folder>\w{1,3})\)'
    r'if\(!(?P=lc)\|\|(?P=it)\.toLowerCase\(\)\.includes\((?P=lc)\)\)'
    r'(?P<result>\w{1,3})\.push\(\{path:(?P=it)\+"/",'
    r'name:(?P=it)\.split\("/"\)\.pop\(\)\|\|"",type:"directory"\}\);'
    r'(?P=result)\.sort\(\((?P<sa>\w{1,3}),(?P<sb>\w{1,3})\)=>'
    r'(?:'
    r'(?P=sa)\.type===(?P=sb)\.type\?'
    r'(?P=sa)\.path\.localeCompare\((?P=sb)\.path\):'
    r'(?P=sa)\.type==="directory"\?-1:1'
    r'|'
    r'(?P=sa)\.path\.localeCompare\((?P=sb)\.path\)'
    r'\)'
    r')'
)
# Captures the vscode module name and the exclusion options var from the
# nearest preceding `await <m>.workspace.findFiles(<glob>, <opts>, 100)` —
# both live in the same function as the dir-sort fragment.
IW_VSCODE_OPTS_RE = re.compile(
    r'\bawait\s+(\w{1,3})\.workspace\.findFiles\(\w{1,3},(\w{1,3}),100\)'
)
IW_DIR_SORT_PATCHED_MARKER = 'workspace.findFiles("**/*"+'

# --- Fix Dp4 basename for directory paths ------------------------------------
# Dp4() maps each candidate path to {path, filename, testPenalty}. For directory
# paths derived from Op4() they always end in "/" (e.g. "fb-email-slurper-3000/").
# path.basename("fb-email-slurper-3000/") returns "" (empty string) because Node
# path.basename strips a trailing slash first and then finds nothing.
#
# Fuse.js weights filename at 2x and path at 1x. With filename="" the score is:
#   (path_score*1 + 1.0*2) / 3 ≈ 0.7  →  above threshold=0.5  →  REJECTED.
#
# Fix: strip the trailing "/" before calling basename so directories get their
# correct short name ("fb-email-slurper-3000" instead of "").
#
# 2026-05-07: converted from literal string match to regex with backrefs so
# it survives the per-release minifier rename. Originally hardcoded `pT` for
# the path module; 2.1.132 renamed it to `lT`. The pattern now captures
# whichever 1–3 char identifier the minifier picked and reuses it in the
# replacement.
DP4_BASENAME_RE = re.compile(
    r'(\w{1,3})=(\w{1,3})\.map\(\((\w{1,3})\)=>'
    r'\(\{path:\3,filename:(\w{1,3})\.basename\(\3\),'
    r'testPenalty:\3\.includes\("test"\)\?1:0\}\)\)'
)
# Patched-form detector: the strip-trailing-slash dance is unique to our
# patch. Identifiers are wildcarded so this matches regardless of which
# letters the next minifier picks.
DP4_BASENAME_PATCHED_RE = re.compile(
    r'filename:\w{1,3}\.basename\('
    r'\w{1,3}\.endsWith\("/"\)\?\w{1,3}\.slice\(0,-1\):\w{1,3}\),'
)

# --- Folder-first sort in @ mention dropdown ---------------------------------
# After the folder-search patch, results are sorted purely alphabetically.
# Patch changes the final sort so directories come before files, and within
# each group results sort by BASENAME (last path segment) rather than full
# path. The basename change matters: typing @freeflow-d should surface
# freeflow-dev/ at the top, not a deeper folder like
# image-create/doodle/freeflow-dev-icon/ whose path prefix sorts earlier.
# With basename sort, "freeflow-dev" wins over "freeflow-dev-icon"
# alphabetically — the shorter exact-prefix folder rises to #1.
#
# The match accepts either the upstream original (pure-alphabetical sort) or
# the type-priority intermediate form left behind by folder-search v1+, and
# captures the three rotating identifiers (sort param a, sort param b, result
# array). Same self-healing approach as IW_DIR_SORT_RE — identifier rotations
# do not require code changes.
IW_SORT_RE = re.compile(
    r'(?P<result>\w{1,3})\.sort\(\((?P<sa>\w{1,3}),(?P<sb>\w{1,3})\)=>'
    r'(?:'
    # upstream type-priority form
    r'(?P=sa)\.type===(?P=sb)\.type\?'
    r'(?P=sa)\.path\.localeCompare\((?P=sb)\.path\):'
    r'(?P=sa)\.type==="directory"\?-1:1'
    r'|'
    # upstream pure-alphabetical form
    r'(?P=sa)\.path\.localeCompare\((?P=sb)\.path\)'
    r'|'
    # this patcher's earlier basename-only form — matched so the relevance
    # upgrade can replace it in place, no backup restore needed
    r'\{if\((?P=sa)\.type!==(?P=sb)\.type\)return (?P=sa)\.type==="directory"\?-1:1;'
    r'let _bn=p=>\{let t=p\.endsWith\("/"\)\?p\.slice\(0,-1\):p;return t\.split\("/"\)\.pop\(\)\};'
    r'return _bn\((?P=sa)\.path\)\.localeCompare\(_bn\((?P=sb)\.path\)\)\}'
    r')'
    r'\)\}catch\{\}return\s+(?P=result)\}'
)

# Marker proving the current ranked-and-filtered sort is present. A bundle
# carrying any older sort form will NOT contain it. Bumped to v3 when the
# dropdown started dropping directories that do not match the query.
IW_SORT_PRIORITY_MARKER = '/*__cce_relrank3__*/'

# The lowercased-query var, declared `let <x>=<y>?.toLowerCase();` at the head
# of the IW dir block. folder-sort-priority closes its comparator over this so
# it can rank each folder by how well its name matches what the user typed.
IW_LC_LOOKBACK_RE = re.compile(r'let\s+(\w{1,3})=\w{1,3}\?\.toLowerCase\(\);')

# --- Open session as next tab at far right (not split pane) -------------------
# When opening a session from the sessions list and no existing Claude tab group
# is found, the stock code calls findUnusedColumn() which creates a new split
# pane. Instead, find the rightmost non-empty tab group and open there so the
# session lands to the right of everything else rather than wherever focus is.
# Falls back to ViewColumn.Beside (new split to the right) only if no tab
# groups exist at all (e.g. fresh empty VS Code window).
#
# Two originals are handled: the stock bundle form and the v1 patched form
# (ViewColumn.Active) so re-patching after a version bump works either way.
# NOTE: as of 2.1.128 the vscode module reference in this region is `C0` and
# the createPanel-local "started in new column" flag is `N`. Older bundles used
# `b0` and `x` respectively. If a future release renames the module again,
# refresh PATCHED_SESSION_SPLIT (two `C0` references) and ORIGINAL_SESSION_SPLIT
# (the trailing flag char) together. The V1 fallback string only catches a
# stale already-patched form on disk; harmless to leave even when its module
# name no longer matches the surrounding scope.
ORIGINAL_SESSION_SPLIT_V1 = 'else G=b0.ViewColumn.Active'
# Generic regex form — anchors on `findUnusedColumn()` and captures both the
# column variable (group 'colvar') and the flag variable (group 'flagvar').
# Survives the build-to-build minified rename churn (G/x/N swap pattern seen
# across 2.1.126 → 2.1.138). The vscode module identifier is captured separately
# below from the adjacent `.window.createWebviewPanel("claudeVSCodePanel"...)`
# call so we can rewrite ViewColumn.Beside against the right import alias.
SESSION_SPLIT_RE = re.compile(
    r'else\s+(?P<colvar>[A-Za-z_$][\w$]*)\s*=\s*this\.findUnusedColumn\(\)\s*,\s*'
    r'(?P<flagvar>[A-Za-z_$][\w$]*)\s*=\s*!0'
)
SESSION_SPLIT_MODULE_RE = re.compile(
    r'([A-Za-z_$][\w$]*)\.window\.createWebviewPanel\("claudeVSCodePanel"'
)
SESSION_SPLIT_PATCHED_MARKER = '_tg.reduce((a,c)=>a.viewColumn>c.viewColumn'

# --- Strip broken moveEditorToEndOfGroup call -------------------------------
# The Remote-SSH bundle of 2.1.126 (and possibly later builds) registers the
# claude-vscode.editor.open command with a trailing call to
#   workbench.action.moveEditorToEndOfGroup
# That command does not exist in VS Code, so every "Open in New Tab" invocation
# pops an error dialog. The local-install bundle for the SAME version label
# does NOT contain this call — Anthropic ships two different artifacts under
# 2.1.126 depending on Remote-SSH vs local install.
#
# Match on the broken command name so the patch survives minified identifier
# changes (the vscode import alias is currently I4 but renames build-to-build).
# Drop the leading semicolon too so the surrounding statement still terminates
# cleanly.
BROKEN_MOVE_EDITOR_PATTERN = re.compile(
    r';await [A-Za-z_$][A-Za-z0-9_$]*\.commands\.executeCommand'
    r'\("workbench\.action\.moveEditorToEndOfGroup"\)'
)


def patch_strip_broken_move_editor(text: str) -> tuple[str, list[str], list[str]]:
    """Remove the bogus workbench.action.moveEditorToEndOfGroup call."""
    applied: list[str] = []
    skipped: list[str] = []
    matches = list(BROKEN_MOVE_EDITOR_PATTERN.finditer(text))
    if not matches:
        skipped.append("strip-broken-move-editor (not present)")
        return text, applied, skipped
    new_text = BROKEN_MOVE_EDITOR_PATTERN.sub("", text)
    applied.append(f"strip-broken-move-editor ({len(matches)} site)")
    return new_text, applied, skipped


def patch_title_unlock(text: str) -> tuple[str, list[str], list[str]]:
    """Drop the 300px cap on the active-session title bar.

    The header is `display:flex` with a sibling `headerSpacer` set to flex:1,
    so removing just the cap on titleGroup lets the title use as much
    horizontal room as is free without disturbing the right-side buttons.
    Single-line ellipsis behavior in titleTextInner is left intact so the
    rare title that exceeds the full header width still clips cleanly
    instead of wrapping the header onto two rows.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    if TITLE_UNLOCK_PATCHED_MARKER in new_text:
        skipped.append("title-unlock (already)")
        return new_text, applied, skipped

    if ORIGINAL_TITLE_GROUP_RULE not in new_text:
        skipped.append("title-unlock (titleGroup rule not found)")
        return new_text, applied, skipped

    new_text = new_text.replace(
        ORIGINAL_TITLE_GROUP_RULE, PATCHED_TITLE_GROUP_RULE, 1
    )
    applied.append("title-unlock (300px cap dropped)")
    return new_text, applied, skipped


def patch_dynamic_tab_widths(text: str) -> tuple[str, list[str], list[str]]:
    """Append responsive navTabLabel max-width rules keyed on tab count.

    Uses CSS :has() so no JS is needed — the browser recalculates on every
    DOM change. Rules cascade in declaration order so the highest tab-count
    match wins.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    if DYNAMIC_TAB_WIDTH_MARKER in text:
        skipped.append("dynamic-tab-widths (already)")
        return text, applied, skipped

    if ORIGINAL_NAV_TAB_LABEL_RULE not in text:
        skipped.append("dynamic-tab-widths (navTabLabel rule not found)")
        return text, applied, skipped

    new_text = text
    # Strip prior forms in newest-first order so superset rules don't survive.
    if OLD_DYNAMIC_TAB_WIDTH_RULES_V3 in new_text:
        new_text = new_text.replace(OLD_DYNAMIC_TAB_WIDTH_RULES_V3, "", 1)
        applied.append("dynamic-tab-widths (stripped Build 17 tier-by-count form)")
    elif OLD_DYNAMIC_TAB_WIDTH_RULES_V2 in new_text:
        new_text = new_text.replace(OLD_DYNAMIC_TAB_WIDTH_RULES_V2, "", 1)
        applied.append("dynamic-tab-widths (stripped Build 16 single-line form)")
    elif OLD_DYNAMIC_TAB_WIDTH_RULES_V1 in new_text:
        new_text = new_text.replace(OLD_DYNAMIC_TAB_WIDTH_RULES_V1, "", 1)
        applied.append("dynamic-tab-widths (stripped Build 7-15 form)")

    new_text = new_text.rstrip() + "\n" + DYNAMIC_TAB_WIDTH_RULES + "\n"
    applied.append(
        "dynamic-tab-widths (flex 1 1 0 — labels fill pills, share container)"
    )
    return new_text, applied, skipped


def image_pill_rule() -> str:
    """Scoped rule appended to the CSS — applies only when a pill has a
    .thumbIcon child (image kind). Text/document context pills never match
    this selector, so they keep the stock 24px height."""
    return (
        ".pill_lcdCYQ:has(.thumbIcon_lcdCYQ){"
        f"gap:{PILL_GAP}px !important;"
        f"max-width:{PILL_MAX_WIDTH}px !important;"
        f"height:{PILL_HEIGHT}px !important;"
        f"min-height:{PILL_HEIGHT}px !important;}}"
    )


def patched_thumb_rule() -> str:
    return (
        ".thumbIcon_lcdCYQ{object-fit:cover;border-radius:max(1px,"
        "calc(var(--pill-radius) - var(--pill-padding)));flex-shrink:0;"
        f"width:{THUMB_SIZE}px !important;"
        f"height:{THUMB_SIZE}px !important}}"
    )


def patched_remove_button_rule() -> str:
    return ORIGINAL_REMOVE_BUTTON_RULE.replace(
        "cursor:pointer;opacity:0;",
        "cursor:pointer;opacity:1 !important;",
    )


def find_extension_dirs() -> list[Path]:
    found = []
    for pattern in EXTENSION_GLOBS:
        for hit in glob.glob(os.path.expanduser(pattern)):
            p = Path(hit)
            if p.is_dir() and (p / "webview" / "index.css").exists():
                found.append(p)
    return found


def patch_css(ext_dir: Path) -> str:
    css = ext_dir / "webview" / "index.css"
    text = css.read_text()
    new_text = text
    applied: list[str] = []
    skipped: list[str] = []

    # === Patch A: image-pill geometry (scoped via :has(.thumbIcon)) ===
    if IMAGE_PILL_RULE_MARKER in text:
        skipped.append("image-pill (already)")
    else:
        # Step 1: revert any over-broad pill tail to original.
        # Check exact known forms first; fall back to regex for unknown builds
        # (handles intermediate iterations, debug leftover styles, etc.).
        if ORIGINAL_PILL_TAIL not in new_text:
            reverted = False
            for old_tail in (OLD_PATCHED_PILL_TAIL,):
                if old_tail in new_text:
                    new_text = new_text.replace(old_tail, ORIGINAL_PILL_TAIL, 1)
                    applied.append("pill-tail (reverted known over-broad form)")
                    reverted = True
                    break
            if not reverted:
                # Regex fallback: any pill tail that starts with gap: but isn't
                # the original 4px form is a modified build — revert it.
                m = re.search(
                    r'(gap:(?!4px;)[^}]*?transition:border-color \.15s\})',
                    new_text,
                )
                if m:
                    new_text = new_text.replace(m.group(1), ORIGINAL_PILL_TAIL, 1)
                    applied.append("pill-tail (reverted modified form via regex)")

        if OLD_PATCHED_LABEL_RULE in new_text:
            new_text = new_text.replace(
                OLD_PATCHED_LABEL_RULE, ORIGINAL_LABEL_RULE, 1
            )
            applied.append("label (reverted Build 1-2 over-broad form)")

        # Step 2: append the scoped :has rule. Only proceed if the original
        # pill rule is now in the text — that confirms we know the bundle.
        if ORIGINAL_PILL_TAIL in new_text:
            new_text = new_text.rstrip() + "\n" + image_pill_rule() + "\n"
            applied.append("image-pill (scoped via :has(.thumbIcon))")
        else:
            skipped.append(
                "image-pill (unknown bundle — original pill rule not present)"
            )

    # === Patch B: thumbnail image size (only image pills have .thumbIcon) ===
    patched_thumb = patched_thumb_rule()
    if patched_thumb in new_text:
        skipped.append("thumb-icon (already)")
    elif ORIGINAL_THUMB_RULE in new_text:
        new_text = new_text.replace(ORIGINAL_THUMB_RULE, patched_thumb, 1)
        applied.append("thumb-icon")
    else:
        # Regex fallback: find any thumbIcon rule with object-fit:cover and
        # replace it with the original before applying the size patch.
        m = re.search(
            r'\.thumbIcon_lcdCYQ\{object-fit:cover;border-radius:[^;]+;'
            r'flex-shrink:0;[^}]+\}',
            new_text,
        )
        if m and m.group(0) != patched_thumb:
            new_text = new_text.replace(m.group(0), ORIGINAL_THUMB_RULE, 1)
            new_text = new_text.replace(ORIGINAL_THUMB_RULE, patched_thumb, 1)
            applied.append("thumb-icon (reverted modified form + patched)")
        else:
            skipped.append("thumb-icon (unknown bundle)")

    # === Patch C: always-visible remove (X) button ===
    if REMOVE_BUTTON_PATCHED_MARKER in new_text:
        skipped.append("remove-button (already)")
    elif ORIGINAL_REMOVE_BUTTON_RULE in new_text:
        new_text = new_text.replace(
            ORIGINAL_REMOVE_BUTTON_RULE, patched_remove_button_rule(), 1
        )
        applied.append("remove-button")
    else:
        skipped.append("remove-button (unknown bundle)")

    # === Patch D: dynamic nav-tab label widths (shrinks as tab count grows) ===
    new_text, dtw_applied, dtw_skipped = patch_dynamic_tab_widths(new_text)
    applied.extend(dtw_applied)
    skipped.extend(dtw_skipped)

    # === Patch E: drop 300px cap on active-session title bar ===
    new_text, tu_applied, tu_skipped = patch_title_unlock(new_text)
    applied.extend(tu_applied)
    skipped.extend(tu_skipped)

    # === Patch F: timestamp pseudo-element under user message bubbles ===
    new_text, umt_applied, umt_skipped = patch_user_msg_time_css(new_text)
    applied.extend(umt_applied)
    skipped.extend(umt_skipped)

    # === Patch G: cleanup — strip retired star rules ===
    new_text, ats_applied, ats_skipped = patch_active_tab_star(new_text)
    applied.extend(ats_applied)
    skipped.extend(ats_skipped)

    if not applied:
        if all("already" in s for s in skipped):
            return f"css already patched: {css.name}"
        return f"css SKIP ({'; '.join(skipped)}): {css.name}"

    backup = css.with_suffix(f".css.bak.{int(time.time())}")
    shutil.copy(css, backup)
    css.write_text(new_text)
    msg = f"css patched: {css.name} ({', '.join(applied)}"
    if skipped:
        msg += f"; skipped: {', '.join(skipped)}"
    msg += f", backup: {backup.name})"
    return msg


def patch_image_only_submit(text: str) -> tuple[str, list[str], list[str]]:
    """Allow submitting a chat message with only an image and no text.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    if IMAGE_ONLY_PATCHED_MARKER in new_text:
        skipped.append("image-only-submit (already)")
        return new_text, applied, skipped

    # If a prior patched form is present, revert the k1 gate to stock so the
    # standard apply path below replaces it with the new ZWS form. The Enter
    # gate is unchanged between old and new patched forms — leave it alone.
    if BUILD14_PATCHED_K1_GATE in new_text:
        new_text = new_text.replace(BUILD14_PATCHED_K1_GATE, ORIGINAL_K1_GATE, 1)
        applied.append('image-only-submit (reverted Build 14 "." form)')
    elif OLD_PATCHED_K1_GATE in new_text:
        new_text = new_text.replace(OLD_PATCHED_K1_GATE, ORIGINAL_K1_GATE, 1)
        applied.append("image-only-submit (reverted Build 4-13 empty form)")

    enter_ok = ORIGINAL_ENTER_GATE in new_text or PATCHED_ENTER_GATE in new_text
    k1_ok = ORIGINAL_K1_GATE in new_text
    if not (enter_ok and k1_ok):
        bits = []
        if not enter_ok:
            bits.append("enter-gate not found")
        if not k1_ok:
            bits.append("k1-gate not found")
        skipped.append(f"image-only-submit ({'; '.join(bits)})")
        return new_text, applied, skipped

    if ORIGINAL_ENTER_GATE in new_text:
        new_text = new_text.replace(ORIGINAL_ENTER_GATE, PATCHED_ENTER_GATE, 1)
    new_text = new_text.replace(ORIGINAL_K1_GATE, PATCHED_K1_GATE, 1)
    applied.append("image-only-submit (Enter gate + k1 gate + ZWS placeholder)")
    return new_text, applied, skipped


def patch_user_msg_time_css(text: str) -> tuple[str, list[str], list[str]]:
    """Append the timestamp pseudo-element rule for user message bubbles.

    V4: also strips the old V1–V3 block ::after rule from any live bundle
    that still has it, so machines pick up the inline form on the next
    watcher pass without waiting for an Anthropic re-emit.
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    # Cleanup: strip the V1–V3 ::after rule (block under the bubble).
    if USER_MSG_TIME_CSS_OLD_MARKER in new_text:
        # Match the position-relative + ::after pair so we remove the whole
        # block, not just the ::after rule.
        pattern = (
            r'\[class\*="userMessageContainer_"\]\{position:relative\}'
            r'\[class\*="userMessageContainer_"\]::after\{[^}]*\}\n?'
        )
        m = re.search(pattern, new_text)
        if m:
            new_text = new_text.replace(m.group(0), "")
            applied.append("user-msg-time-css (stripped V1–V3 ::after rule)")

    if USER_MSG_TIME_CSS_MARKER in new_text:
        skipped.append("user-msg-time-css (already)")
        return new_text, applied, skipped
    new_text = new_text.rstrip() + "\n" + USER_MSG_TIME_CSS_RULE + "\n"
    applied.append("user-msg-time-css V4 (inline timestamp before bubble text)")
    return new_text, applied, skipped


def patch_active_tab_star(text: str) -> tuple[str, list[str], list[str]]:
    """Strip any leftover star CSS rules from the bundle.

    Build 40 retired the star feature entirely. The Local/Web segmented control
    is gated on `authMethod === "claudeai"` reading from a reactive signal that
    briefly resolves to undefined during webview connection transitions, so the
    tab-anchored star (Build 31) flickered out every time the panel re-mounted.
    The session-row star (Build 28) was an earlier iteration. This function now
    just removes both rules if a prior build wrote them, and adds nothing.
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    # Strip Build 28 session-row star rule.
    if OLD_ACTIVE_SESSION_STAR_PREFIX in new_text:
        m = re.search(
            r'\.sessionItem_OOQiHg\.active_OOQiHg::before\{[^}]*\}\n?',
            new_text,
        )
        if m:
            new_text = new_text.replace(m.group(0), "")
            applied.append("star (stripped Build 28 session-row rule)")

    # Strip Build 31 active-Local/Web-tab star rule.
    if ACTIVE_TAB_STAR_MARKER in new_text:
        m = re.search(
            r'\[class\*="segmented_"\] \[class\*="tab_"\]\[class\*="tabActive_"\]::after\{[^}]*\}\n?',
            new_text,
        )
        if m:
            new_text = new_text.replace(m.group(0), "")
            applied.append("star (stripped Build 31 active-tab rule)")

    if not applied:
        skipped.append("star (already removed)")
    return new_text, applied, skipped


def patch_user_msg_time_js(text: str) -> tuple[str, list[str], list[str]]:
    """Append the MutationObserver IIFE that stamps each new user message.

    Build 31: also strips the V1 IIFE if present, so the duplicate-stamping
    behavior on the live bundle gets cleaned up without waiting for the next
    Anthropic release to re-emit a fresh bundle.
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    # Cleanup: strip any prior IIFE generation that's still in the bundle.
    # V1 = double-stamped, raw ms. V2 = single-stamped, raw ms. V3 = single
    # stamped, natural date + AM/PM (the format James actually asked for).
    for marker_old in USER_MSG_TIME_JS_MARKERS_OLD:
        if marker_old not in new_text:
            continue
        # Marker is e.g. "__cceUserMsgStampInstalled=true" — pull out the
        # version suffix to build a precise regex for that IIFE.
        version = marker_old.split("__cceUserMsgStampInstalled", 1)[1].split("=", 1)[0]
        pattern = (
            r'\n;\(function\(\)\{if\(globalThis\.__cceUserMsgStampInstalled'
            + re.escape(version)
            + r'\)return;.*?\}\)\(\);\n'
        )
        m = re.search(pattern, new_text, re.DOTALL)
        if m:
            new_text = new_text.replace(m.group(0), "")
            label = version if version.startswith("V") else "V1"
            applied.append(f"user-msg-time-js (stripped {label} IIFE)")

    if USER_MSG_TIME_JS_MARKER in new_text:
        skipped.append("user-msg-time-js (already)")
        return new_text, applied, skipped
    new_text = new_text + USER_MSG_TIME_JS_IIFE
    applied.append("user-msg-time-js V5 (inline stamp on inner bubble, with seconds)")
    return new_text, applied, skipped


def patch_full_path_tool_headers(text: str) -> tuple[str, list[str], list[str]]:
    """Show parent folders in tool-call rows instead of just the basename.

    Walks the seven `.split("/").pop()` callsites (Edit/Write/MultiEdit
    header, Read header, batched-Read array, ExitPlanMode, NotebookEdit,
    and two permissionRequest dialogs) and rewrites each to use
    `.slice(-3).join("/")`. Idempotent — bails out early if the marker is
    already present (the marker is the first patched site's full new form).
    """
    applied: list[str] = []
    skipped: list[str] = []

    new_text = text
    hits = 0
    # Site 7 (the `(Z.file_path||"")` permissionRequest pattern) appears twice
    # in the bundle — once for Read, once for Write. Replace ALL instances of
    # each anchor so both Read and Write get patched in a single pass. Per-
    # anchor count gives us idempotency for free: if an anchor is already in
    # its patched form, count == 0 and the loop just moves on.
    for old, new in FULL_PATH_TOOL_HEADER_PATCHES:
        count = new_text.count(old)
        if count > 0:
            new_text = new_text.replace(old, new)
            hits += count

    if hits == 0:
        # Either fully patched already, or none of the anchors are recognizable
        # (Anthropic shipped a re-minified bundle and we need to re-anchor).
        if FULL_PATH_TOOL_HEADER_MARKER in new_text:
            skipped.append("full-path-tool-headers (already)")
        else:
            skipped.append("full-path-tool-headers (no anchors matched — re-anchor needed)")
        return new_text, applied, skipped

    applied.append(f"full-path-tool-headers ({hits} sites patched)")
    return new_text, applied, skipped


def patch_folder_mention(text: str) -> tuple[str, list[str], list[str]]:
    """Make clicking a folder in the @ mention dropdown add a context chip.

    Stock behavior: clicking a directory just fills in the path so you can keep
    typing to narrow down. Patched behavior: directories are treated exactly
    like files — a chip is created and the cursor moves past a trailing space.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    if DIR_MENTION_PATCHED_MARKER in new_text:
        skipped.append("folder-mention (already)")
        return new_text, applied, skipped

    m = DIR_MENTION_RE.search(new_text)
    if not m:
        skipped.append("folder-mention (pattern not found)")
        return new_text, applied, skipped

    helper = m.group("helper")
    replacement = (
        'let m1=!0,_0=d.path+" ",'
        f'k0={helper}(D,L2,_0,!0);if(t.current.textContent=k0,A(k0),m1)'
        'o1((O5)=>new Set(O5).add(`@${d.path}`));'
        'let l0=t.current.firstChild||t.current;if(l0){'
        'let O5=document.createRange(),S2=1,'
    )
    new_text = new_text[:m.start()] + replacement + new_text[m.end():]
    applied.append(f"folder-mention (directories now add context chip; helper={helper})")
    return new_text, applied, skipped


def patch_slash_tab_complete(text: str) -> tuple[str, list[str], list[str]]:
    """Make picker-selecting a slash command insert it into the input
    instead of sending it bare.

    Stock behavior: clicking /foo (or selecting it from the picker and
    pressing Enter) calls C(`/${name}`) immediately — the message fires
    with no chance to add args. /rename-session fires bare, args are
    impossible from the picker UI.

    Patched behavior: the action callback sets the input text to
    `/${name} ` (trailing space), focuses the input, and places the
    cursor at the end. The user can then type args and hit Enter.
    Argless skills cost one extra Enter.

    The else branch is the only one we touch — `/context` and `/usage`
    keep their special-case immediate-fire callbacks.

    The input ref is renamed by the minifier between releases, so we
    discover its current name by reading the stable `mention-file`
    action registration (which uses the same ref).

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    # Idempotence marker — stock bundle never calls setInputText with
    # a slash-command template literal.
    if "setInputText(`/${" in text:
        skipped.append("slash-tab-complete (already)")
        return text, applied, skipped

    # Discover the input ref var by matching the mention-file action.
    ref_match = re.search(
        r'id:"mention-file"[^}]*?\},"Context",\(\)=>\{'
        r'(\w+)\.current\?\.focus\(\),\1\.current\?\.insertAtMention\("@",!0\)\}',
        text,
    )
    if not ref_match:
        skipped.append("slash-tab-complete (input ref not found via mention-file)")
        return text, applied, skipped
    ref_var = ref_match.group(1)

    # Match the slash command else branch:
    #   else e1=()=>void C(`/${x1.name}`);
    slash_match = re.search(
        r'else (\w+)=\(\)=>void (\w+)\(`/\$\{(\w+)\.name\}`\);',
        text,
    )
    if not slash_match:
        skipped.append("slash-tab-complete (else branch pattern not found)")
        return text, applied, skipped

    e_var = slash_match.group(1)
    iter_var = slash_match.group(3)
    repl = (
        f'else {e_var}=()=>{{'
        f'{ref_var}.current?.setInputText(`/${{{iter_var}.name}} `);'
        f'{ref_var}.current?.focus();'
        f'}};'
    )
    new_text = text[:slash_match.start()] + repl + text[slash_match.end():]
    applied.append("slash-tab-complete (insert into input instead of bare-send)")
    return new_text, applied, skipped


COPY_FIX_MARKER = "/*__cce_copy_safety_v1__*/"
COPY_FIX_IIFE = (
    COPY_FIX_MARKER
    + "(function(){try{window.addEventListener('copy',function(e){"
    + "try{var s=window.getSelection&&window.getSelection().toString();"
    + "if(s&&e.clipboardData){e.clipboardData.setData('text/plain',s);"
    + "e.preventDefault();}}catch(_){}}, true);}catch(_){}})();"
)


def patch_copy_fix(text: str) -> tuple[str, list[str], list[str]]:
    if COPY_FIX_MARKER in text:
        return text, [], ["copy-fix (already)"]
    return COPY_FIX_IIFE + text, ["copy-fix"], []


# --- Cmd+A/C/X/V handled manually for webview inputs (upstream #43477) ------
# Build 55's copy-fix reads window.getSelection(), which is always empty for a
# selection *inside* an <input> — so it never covered the in-session title bar
# or any other input field. VS Code also swallows clipboard shortcuts before
# they reach a webview input. This capture-phase keydown listener claims
# Cmd+A/C/X/V whenever an <input> is focused and runs the operation by hand:
# select() for A, navigator.clipboard for C/X/V. Scoped to <input> only —
# <textarea>/contentEditable (the chat composer) are left alone so native
# image-paste keeps working. Non-input contexts fall through to copy-fix.
CLIPBOARD_KEYS_MARKER = "/*__cce_clipboard_keys_v1__*/"
CLIPBOARD_KEYS_IIFE = (
    CLIPBOARD_KEYS_MARKER
    + "(function(){try{"
    + "function _ed(){var el=document.activeElement;"
    + "return el&&el.tagName==='INPUT'?el:null;}"
    + "function _set(el,v){try{"
    + "var d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');"
    + "if(d&&d.set)d.set.call(el,v);else el.value=v;"
    + "el.dispatchEvent(new Event('input',{bubbles:true}));"
    + "}catch(_){}}"
    + "window.addEventListener('keydown',function(e){try{"
    + "if(!e.metaKey||e.altKey||e.ctrlKey||e.repeat)return;"
    + "var k=(e.key||'').toLowerCase();"
    + "if(k!=='a'&&k!=='c'&&k!=='x'&&k!=='v')return;"
    + "var el=_ed();if(!el)return;"
    + "var s=el.selectionStart,n=el.selectionEnd;"
    + "if(s==null||n==null){s=0;n=(el.value||'').length;}"
    + "e.preventDefault();e.stopImmediatePropagation();"
    + "if(k==='a'){el.select();return;}"
    + "if(k==='c'){var t=el.value.substring(s,n);"
    + "if(t)navigator.clipboard.writeText(t).catch(function(){});return;}"
    + "if(k==='x'){var t=el.value.substring(s,n);if(t){"
    + "navigator.clipboard.writeText(t).catch(function(){});"
    + "_set(el,el.value.slice(0,s)+el.value.slice(n));"
    + "try{el.selectionStart=el.selectionEnd=s;}catch(_){}}return;}"
    + "if(k==='v'){navigator.clipboard.readText().then(function(t){"
    + "if(t==null)return;"
    + "_set(el,el.value.slice(0,s)+t+el.value.slice(n));"
    + "try{var c=s+t.length;el.selectionStart=el.selectionEnd=c;}catch(_){}"
    + "}).catch(function(){});return;}"
    + "}catch(_){}},true);"
    + "}catch(_){}})();"
)


def patch_clipboard_keys(text: str) -> tuple[str, list[str], list[str]]:
    if CLIPBOARD_KEYS_MARKER in text:
        return text, [], ["clipboard-keys (already)"]
    return CLIPBOARD_KEYS_IIFE + text, ["clipboard-keys"], []


# --- Image too large gate + honest rejection banner -------------------------
# Claude's API rejects any image whose base64 payload tops 10,485,760 bytes.
# The stock extension never checks before attaching, and its rejection banner
# only knows the phrase "unsupported file type" — so an oversized PNG either
# fails downstream or gets mislabelled. Two surgical edits to webview JS:
#
#   ux()  the single attach choke point every path runs through (drag-drop,
#         file picker, paste, + button). Measure each image's base64 length
#         and divert anything over the ceiling into the unsupported list,
#         tagged {oversize:!0,name,mb}, instead of attaching it.
#   t()   the rejection banner. Split its input into oversize vs genuinely
#         unsupported files and emit the right sentence for each group.
#
# Block, not auto-resize: resizing would silently rewrite the image and guess
# how much quality the user will accept. Blocking keeps them in control.
#
# Identifiers are wildcarded so the edit survives re-minification. This was
# hand-patched into a live bundle on 2026-05-21 but never written back here,
# so every Anthropic re-emit + watcher re-patch since reverted it.
_IMG_GATE_V = r"[A-Za-z_$][\w$]*"

IMAGE_GATE_MARKER = "ccB64"
IMAGE_BANNER_MARKER = "too large to send"

# {J.push(Y.name);continue}try{let X=await u20(Y);Z.push({file:Y,dataUrl:X})}
#  1=unsupported-list 2=file 3=dataUrl 4=processor 5=attachments-list
ATTACH_GATE_RE = re.compile(
    r"\{(" + _IMG_GATE_V + r")\.push\((" + _IMG_GATE_V + r")\.name\);continue\}"
    r"try\{let (" + _IMG_GATE_V + r")=await (" + _IMG_GATE_V + r")\(\2\);"
    r"(" + _IMG_GATE_V + r")\.push\(\{file:\2,dataUrl:\3\}\)\}"
)

# function t(x1){if(x1.length===0)return;let e1=...;j(`Unsupported file type...`)}
#  1=name 2=arg 3=joined-var 4=display-fn
IMAGE_BANNER_RE = re.compile(
    r"function (" + _IMG_GATE_V + r")\((" + _IMG_GATE_V + r")\)\{"
    r"if\(\2\.length===0\)return;"
    r"let (" + _IMG_GATE_V + r")=\2\.length<=3\?\2\.join\(\", \"\):"
    r"`\$\{\2\.slice\(0,3\)\.join\(\", \"\)\} and \$\{\2\.length-3\} more`;"
    r"(" + _IMG_GATE_V + r")\("
    r"`Unsupported file type\$\{\2\.length>1\?\"s\":\"\"\}: \$\{\3\}\. "
    r"Supported types: images \(PNG, JPG, GIF, WebP\), text files, and PDFs\. "
    r"To reference unsupported files, please include its absolute file path in your prompt "
    r"\(using @ for paths inside your working directory, and entering the path for files outside\)\.`\)\}"
)


def patch_image_size_gate(text: str) -> tuple[str, list[str], list[str]]:
    """Block oversized images at attach time and tell the truth about why.

    Two independent edits — the ux() attach gate and the t() rejection
    banner — each idempotent on its own marker, so a half-applied bundle
    still converges on the next run.
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    # --- Edit 1: 10 MB base64 gate at the ux() attach choke point ---
    if IMAGE_GATE_MARKER in new_text:
        skipped.append("image-gate (already)")
    else:
        def _gate(m):
            uns, f, du, proc, att = m.group(1, 2, 3, 4, 5)
            return (
                "{" + uns + ".push(" + f + ".name);continue}"
                "try{let " + du + "=await " + proc + "(" + f + ");"
                "let ccB64=typeof " + du + '==="string"&&' + f + ".type&&"
                + f + '.type.startsWith("image/")?'
                + du + ".length-" + du + '.indexOf(",")-1:0;'
                "if(ccB64>10485760){" + uns + ".push({oversize:!0,name:"
                + f + ".name,mb:(ccB64/1048576).toFixed(1)});continue}"
                + att + ".push({file:" + f + ",dataUrl:" + du + "})}"
            )

        candidate, n = ATTACH_GATE_RE.subn(_gate, new_text)
        if n == 1:
            new_text = candidate
            applied.append("image-gate (10 MB base64 ceiling at the ux attach choke point)")
        elif n == 0:
            skipped.append("image-gate (attach choke point not found — re-anchor needed)")
        else:
            skipped.append(f"image-gate (ambiguous — {n} attach sites, not applied)")

    # --- Edit 2: honest oversize/unsupported split in the t() banner ---
    if IMAGE_BANNER_MARKER in new_text:
        skipped.append("image-banner (already)")
    else:
        def _banner(m):
            name, arg, fn = m.group(1), m.group(2), m.group(4)
            return (
                "function " + name + "(" + arg + "){if(" + arg + ".length===0)return;"
                "let ccBig=" + arg + ".filter((n)=>n&&n.oversize),"
                "ccBad=" + arg + ".filter((n)=>!(n&&n.oversize)),ccMsg=[];"
                "if(ccBig.length>0){let nm=ccBig.map((b)=>`${b.name} (${b.mb} MB)`),"
                'e1=nm.length<=3?nm.join(", "):`${nm.slice(0,3).join(", ")} and ${nm.length-3} more`;'
                'ccMsg.push(`${ccBig.length>1?"Those images are":"That image is"} '
                "too large to send. Claude's API caps images at 10 MB once base64-encoded: "
                '${e1}. Resize ${ccBig.length>1?"them":"it"} smaller and attach again.`)}'
                'if(ccBad.length>0){let e1=ccBad.length<=3?ccBad.join(", "):'
                '`${ccBad.slice(0,3).join(", ")} and ${ccBad.length-3} more`;'
                'ccMsg.push(`Unsupported file type${ccBad.length>1?"s":""}: ${e1}. '
                "Supported types: images (PNG, JPG, GIF, WebP), text files, and PDFs. "
                "To reference unsupported files, please include its absolute file path in your prompt "
                "(using @ for paths inside your working directory, and entering the path for files outside).`)}"
                + fn + '(ccMsg.join(" "))}'
            )

        candidate, n = IMAGE_BANNER_RE.subn(_banner, new_text)
        if n == 1:
            new_text = candidate
            applied.append("image-banner (oversize vs unsupported split in the t rejection banner)")
        elif n == 0:
            skipped.append("image-banner (t banner not found — re-anchor needed)")
        else:
            skipped.append(f"image-banner (ambiguous — {n} banner sites, not applied)")

    return new_text, applied, skipped


def patch_js(ext_dir: Path) -> str:
    js = ext_dir / "webview" / "index.js"
    if not js.exists():
        return f"js SKIP (no index.js): {js}"
    text = js.read_text()
    applied: list[str] = []
    skipped: list[str] = []

    # --- Patch: capture-phase copy safety net (upstream #43477) ---
    text, cf_applied, cf_skipped = patch_copy_fix(text)
    applied.extend(cf_applied)
    skipped.extend(cf_skipped)

    # --- Patch: Cmd+A/C/X/V handled manually for webview inputs (#43477) ---
    text, ck_applied, ck_skipped = patch_clipboard_keys(text)
    applied.extend(ck_applied)
    skipped.extend(ck_skipped)

    # --- Patch: image-only submit ---
    text, img_applied, img_skipped = patch_image_only_submit(text)
    applied.extend(img_applied)
    skipped.extend(img_skipped)

    # --- Patch: block oversized images + honest rejection banner ---
    text, isg_applied, isg_skipped = patch_image_size_gate(text)
    applied.extend(isg_applied)
    skipped.extend(isg_skipped)

    # --- Patch: folder @mention adds chip ---
    text, dir_applied, dir_skipped = patch_folder_mention(text)
    applied.extend(dir_applied)
    skipped.extend(dir_skipped)

    # --- Patch: parent folders in tool-call header rows ---
    text, fp_applied, fp_skipped = patch_full_path_tool_headers(text)
    applied.extend(fp_applied)
    skipped.extend(fp_skipped)

    # --- Patch: stamp each new userMessageContainer with arrival timestamp ---
    text, umt_applied, umt_skipped = patch_user_msg_time_js(text)
    applied.extend(umt_applied)
    skipped.extend(umt_skipped)

    # --- Patch: tab-complete slash command into input (no bare-send) ---
    text, stc_applied, stc_skipped = patch_slash_tab_complete(text)
    applied.extend(stc_applied)
    skipped.extend(stc_skipped)

    # --- Patch: session-delete behavior (filter by ID + prune in doListSessions) ---
    text, sd_applied, sd_skipped = patch_session_delete_fix(text)
    applied.extend(sd_applied)
    skipped.extend(sd_skipped)

    # --- Patch: Cmd+click gate on tool body rows ---
    if JS_PATCHED_MARKER in text:
        skipped.append("click-gate (already)")
    else:
        cond_matches = list(JS_TOOL_ROW_COND_PATTERN.finditer(text))
        uncond_matches = list(JS_TOOL_ROW_UNCOND_PATTERN.finditer(text))
        arrow_matches = list(JS_ARROW_VOID_OPENCONTENT_PATTERN.finditer(text))
        repl_matches = list(JS_REPL_PATTERN.finditer(text))
        diag_matches = list(JS_DIAGNOSTICS_PATTERN.finditer(text))
        if not (cond_matches or uncond_matches or arrow_matches or repl_matches or diag_matches):
            skipped.append("click-gate (no recognised pattern found)")
        else:
            def repl_cond(m):
                var = m.group(2)
                return (
                    f'{m.group(1)}'
                    f'onClick:{var}?(e)=>{{if(e.metaKey||e.ctrlKey){var}(e);}}:void 0,'
                    f'style:{var}?{{cursor:"default"}}:void 0'
                )

            def repl_uncond(m):
                var = m.group(2)
                return (
                    f'{m.group(1)}'
                    f'onClick:(e)=>{{if(e.metaKey||e.ctrlKey){var}(e);}},'
                    f'style:{{cursor:"default"}}'
                )

            def repl_arrow(m):
                return f'(e)=>{{if(e.metaKey||e.ctrlKey){m.group(1)}}}'

            def repl_repl(m):
                return (
                    f'(Q)=>{{if(!Q.metaKey&&!Q.ctrlKey)return;'
                    f'Q.stopPropagation();Q.preventDefault();{m.group(1)}}}'
                )

            def repl_diag(m):
                return (
                    'function J(e){if(!(e&&(e.metaKey||e.ctrlKey)))return;'
                    'let Y=RN0($);Z.fileOpener.openContent(Y,'
                    '"Diagnostics: VSCode Problems",!1)}'
                )

            text = JS_TOOL_ROW_COND_PATTERN.sub(repl_cond, text)
            text = JS_TOOL_ROW_UNCOND_PATTERN.sub(repl_uncond, text)
            text = JS_ARROW_VOID_OPENCONTENT_PATTERN.sub(repl_arrow, text)
            text = JS_REPL_PATTERN.sub(repl_repl, text)
            text = JS_DIAGNOSTICS_PATTERN.sub(repl_diag, text)
            total = (
                len(cond_matches) + len(uncond_matches)
                + len(arrow_matches) + len(repl_matches) + len(diag_matches)
            )
            applied.append(
                f"click-gate ({total} sites — "
                f"{len(cond_matches)} cond, {len(uncond_matches)} uncond, "
                f"{len(arrow_matches)} arrow, {len(repl_matches)} repl, "
                f"{len(diag_matches)} diag)"
            )

    if not applied:
        if all("already" in s for s in skipped):
            return f"js already patched: {js.name}"
        return f"js SKIP ({'; '.join(skipped)}): {js.name}"

    backup = js.with_suffix(f".js.bak.{int(time.time())}")
    shutil.copy(js, backup)
    js.write_text(text)
    msg = f"js patched: {js.name} ({', '.join(applied)}"
    if skipped:
        msg += f"; skipped: {', '.join(skipped)}"
    msg += f", backup: {backup.name})"
    return msg


SESSION_DELETE_FIX_MARKER = "/*__cce_session_fix_v1__*/"

# Anchor: the local filter inside `async deleteSession($)`. Matches identity
# compare `J!==$` and rewrites it to compare by `sessionId.value`.
_VAR = r'[A-Za-z_$][\w$]*'
SESSION_DELETE_FILTER_PATTERN = re.compile(
    rf'(async deleteSession\()({_VAR})(\)\{{if\(this\.sessions\.value=this\.sessions\.value\.filter\(\()({_VAR})(\)=>)\4!==\2\)'
)

# Anchor: head of `async doListSessions()` capturing the connection var, the
# server response var, and the working-list var so we can splice a prune step.
DOLIST_HEAD_PATTERN = re.compile(
    rf'(async doListSessions\(\)\{{let )({_VAR})(=await this\.getConnection\(\),)'
    rf'({_VAR})(=await \2\.listSessions\(\),{_VAR}=\2\.config\.value\?\.initialPermissionMode,)'
    rf'({_VAR})(=\[\.\.\.this\.sessions\.value\],{_VAR}=!1,{_VAR}=BB1\(\);)'
)


def patch_session_delete_fix(text: str) -> tuple[str, list[str], list[str]]:
    """Make the trash icon actually remove rows from the visible session list.

    Two distinct bugs in Anthropic's webview cause the trash button to look
    broken:

    1. The local cache filter in `deleteSession` compares object identity
       (`J!==$`). When references diverge across renders, the filter is a
       no-op and the row stays visible. We rewrite to compare
       `J.sessionId.value !== $.sessionId.value` — bulletproof.

    2. `doListSessions` is additive only. Once a session is in the local
       cache, it stays even after the server filters it out via
       `hiddenSessionIds`. We splice a prune step at the top: drop any
       cached entry whose id is no longer in the server response (preserving
       sessions that haven't been assigned an id yet — the in-flight new
       session case).

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    if SESSION_DELETE_FIX_MARKER in text:
        skipped.append("session-delete-fix (already)")
        return text, applied, skipped

    parts: list[str] = []

    a_match = SESSION_DELETE_FILTER_PATTERN.search(text)
    if not a_match:
        skipped.append("session-delete-fix A (filter pattern not found)")
    else:
        param = a_match.group(2)
        cb = a_match.group(4)
        replacement = (
            f'{a_match.group(1)}{param}{a_match.group(3)}{cb}{a_match.group(5)}'
            f'{cb}.sessionId.value!=={param}.sessionId.value){SESSION_DELETE_FIX_MARKER}'
        )
        text = text[:a_match.start()] + replacement + text[a_match.end():]
        parts.append("filter-by-id")

    b_match = DOLIST_HEAD_PATTERN.search(text)
    if not b_match:
        skipped.append("session-delete-fix B (doListSessions pattern not found)")
    else:
        z_var = b_match.group(4)  # listSessions response
        y_var = b_match.group(6)  # working list
        inject = (
            f'{y_var}={y_var}.filter(__it=>!__it.sessionId.value||'
            f'{z_var}.sessions.some(__s=>__s.id===__it.sessionId.value));'
            f'{SESSION_DELETE_FIX_MARKER}'
        )
        insert_pos = b_match.end()
        text = text[:insert_pos] + inject + text[insert_pos:]
        parts.append("prune-stale")

    if parts:
        applied.append(f"session-delete-fix ({', '.join(parts)})")
    return text, applied, skipped


def patch_folder_sort_priority(text: str) -> tuple[str, list[str], list[str]]:
    """Sort the @ mention dropdown: directories first, then by relevance.

    The upstream sort is purely alphabetical, which buries the folder you
    actually want under config and build-output noise. The patched sort
    builds a comparison key per entry and ranks on, in order:

      1. type        — directories before files
      2. match tier  — exact basename match, then prefix, then mid-string,
                       then no match, against the lowercased query
      3. noise       — clean paths before "noisy" ones (any path segment
                       that is a dotfolder or a build-output dir:
                       node_modules / dist / build / coverage)
      4. depth       — shallower paths before deeply nested ones
      5. basename    — alphabetical tiebreak

    Directories whose basename does not contain the query at all are
    dropped from the list entirely — no match, no show. Files are left
    alone. So `@vscode-e` shows only vscode-extension/, not the .vscode/
    config dirs or the unrelated *-vs-* folders.

    The lowercased-query var is found via a bounded lookback for the
    `let <x>=<y>?.toLowerCase();` line at the head of the IW dir block, so the
    key function can close over it. If it cannot be found the match tier
    collapses to 0 for everything and the sort still ranks by
    type / noise / depth / basename.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    if IW_SORT_PRIORITY_MARKER in text:
        skipped.append("folder-sort-priority (already)")
        return text, applied, skipped

    m = IW_SORT_RE.search(text)
    if not m:
        skipped.append("folder-sort-priority (sort pattern not found)")
        return text, applied, skipped

    result = m.group("result")
    sa = m.group("sa")
    sb = m.group("sb")

    # Discover the lowercased-query var via a bounded lookback; the closest
    # `?.toLowerCase()` declaration before the sort is the IW dir-block one.
    lc = None
    lookback = text[max(0, m.start() - 3000):m.start()]
    lc_hits = list(IW_LC_LOOKBACK_RE.finditer(lookback))
    if lc_hits:
        lc = lc_hits[-1].group(1)

    q_init = f'({lc}||"")' if lc else '""'
    note = (f"relevance + noise ranked; query var={lc}" if lc
            else "noise ranked (query var not found)")

    # Helpers are hoisted out of the comparator so they are built once, not
    # on every comparison. _cc-prefixed names cannot collide with the
    # bundle's single-letter minified identifiers.
    patched = (
        f'let _ccNB=new Set(["node_modules","dist","build","coverage"]);'
        f'let _ccNoisy=p=>p.split("/").some(s=>s&&(s[0]==="."||_ccNB.has(s)));'
        f'let _ccBn=p=>{{let t=p.endsWith("/")?p.slice(0,-1):p;'
        f'return(t.split("/").pop()||"").toLowerCase()}};'
        f'let _ccQ={q_init};'
        f'let _ccRk=n=>!_ccQ?0:n===_ccQ?0:n.startsWith(_ccQ)?1:n.includes(_ccQ)?2:3;'
        f'let _ccKey=o=>{{let n=_ccBn(o.path);return['
        f'o.type==="directory"?0:1,_ccRk(n),_ccNoisy(o.path)?1:0,'
        f'o.path.split("/").length,n]}};'
        f'if(_ccQ)for(let _i={result}.length-1;_i>=0;_i--){{'
        f'let _o={result}[_i];'
        f'if(_o.type==="directory"&&!_ccBn(_o.path).includes(_ccQ))'
        f'{result}.splice(_i,1)}}'
        f'{result}.sort(({sa},{sb})=>{{'
        f'let _ka=_ccKey({sa}),_kb=_ccKey({sb});'
        f'for(let i=0;i<4;i++)if(_ka[i]!==_kb[i])return _ka[i]-_kb[i];'
        f'return _ka[4].localeCompare(_kb[4])}})'
        f'{IW_SORT_PRIORITY_MARKER}}}catch{{}}return {result}}}'
    )
    new_text = text[:m.start()] + patched + text[m.end():]
    applied.append(
        f"folder-sort-priority (directories first, {note}; "
        f"captured sort=({sa},{sb}) result={result})"
    )
    return new_text, applied, skipped


def patch_dp4_basename(text: str) -> tuple[str, list[str], list[str]]:
    """Fix empty filename for directory paths in Dp4().

    Dp4 maps candidates to {path, filename} using path.basename(N). For
    directory paths ending in "/" (all entries from Op4), basename returns "".
    Fuse.js has filename at double weight, so the empty string drives the score
    above threshold=0.5 even when the path is an exact match — the folder is
    filtered out completely. Fix: strip trailing "/" before calling basename.

    Regex-based so the per-release minifier rename (pT → lT → whatever next)
    does not break us. We capture the four identifiers and reuse them in the
    replacement.
    """
    applied: list[str] = []
    skipped: list[str] = []

    if DP4_BASENAME_PATCHED_RE.search(text):
        skipped.append("dp4-basename (already)")
        return text, applied, skipped

    m = DP4_BASENAME_RE.search(text)
    if not m:
        skipped.append("dp4-basename (pattern not found)")
        return text, applied, skipped

    var_x, var_v, var_n, var_path = m.group(1), m.group(2), m.group(3), m.group(4)
    repl = (
        f'{var_x}={var_v}.map(({var_n})=>'
        f'({{path:{var_n},'
        f'filename:{var_path}.basename({var_n}.endsWith("/")?{var_n}.slice(0,-1):{var_n}),'
        f'testPenalty:{var_n}.includes("test")?1:0}}))'
    )
    new_text = text[:m.start()] + repl + text[m.end():]
    applied.append("dp4-basename (strip trailing / so folders get correct filename)")
    return new_text, applied, skipped


def patch_folder_search(text: str) -> tuple[str, list[str], list[str]]:
    """Make the @ mention dropdown find folders by their own name.

    Stock behavior: IW() derives folder results only from the parent paths of
    files whose FILENAME matches the query. Typing @vscode-exten finds
    screenshot PNGs but never surfaces the vscode-extension/ folder because
    its children (build.txt, etc.) do not contain that string in their name.

    Patch: after the existing file-derived directory pass, run a second
    findFiles("**/*<query>*/**") to find any file inside a folder whose name
    matches, then extract and add those folder segments as directory results.

    Regex-based so the minifier rotating single-letter identifiers across
    releases does not break the patch — captured idents are reused in the
    replacement. Structural changes still need a human.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    if IW_DIR_SORT_PATCHED_MARKER in text:
        skipped.append("folder-search (already)")
        return text, applied, skipped

    m = IW_DIR_SORT_RE.search(text)
    if not m:
        skipped.append("folder-search (IW dir-sort pattern not found)")
        return text, applied, skipped

    # Find vscode module name + exclusion options var by looking back through
    # the same function. The function shape is:
    #   try{N=await <vscode>.workspace.findFiles(K,<opts>,100)}
    #   catch{N=await <vscode>.workspace.findFiles(K,<DEFAULT_EXCLUDE_CONST>,100)}
    # We want the TRY branch (real opts var), not the CATCH fallback constant.
    # The try is first in source order so the first match within the bounded
    # lookback wins.
    lookback_start = max(0, m.start() - 3000)
    prelude_matches = list(IW_VSCODE_OPTS_RE.finditer(text, lookback_start, m.start()))
    if not prelude_matches:
        skipped.append("folder-search (vscode module / opts var not found in prelude)")
        return text, applied, skipped
    vscode = prelude_matches[0].group(1)
    opts = prelude_matches[0].group(2)

    lc = m.group("lc")
    q = m.group("q")
    it = m.group("it")
    folder = m.group("folder")
    result = m.group("result")
    sa = m.group("sa")
    sb = m.group("sb")

    # Double-underscore prefixed inner vars cannot collide with single-letter
    # minified idents in the surrounding scope.
    patched = (
        f'let {lc}={q}?.toLowerCase();'
        f'for(let {it} of {folder})if(!{lc}||{it}.toLowerCase().includes({lc}))'
        f'{result}.push({{path:{it}+"/",name:{it}.split("/").pop()||"",type:"directory"}});'
        f'if({q}&&{lc}){{try{{'
        f'let __Y=await {vscode}.workspace.findFiles("**/*"+{lc}+"*/**",{opts},200);'
        f'for(let __L of __Y){{'
        f'let __D={vscode}.workspace.asRelativePath(__L).replaceAll("\\\\","/"),'
        f'__R=__D.split("/"),__P="";'
        f'for(let __i=0;__i<__R.length-1;__i++){{'
        f'__P=__P?__P+"/"+__R[__i]:__R[__i];'
        f'if(__R[__i].toLowerCase().includes({lc})&&!{folder}.has(__P)){{'
        f'{folder}.add(__P),{result}.push({{path:__P+"/",name:__R[__i],type:"directory"}})'
        f'}}}}}}'
        f'}}catch{{}}}}'
        f'{result}.sort(({sa},{sb})=>{sa}.type==={sb}.type?'
        f'{sa}.path.localeCompare({sb}.path):'
        f'{sa}.type==="directory"?-1:1)'
    )
    new_text = text[:m.start()] + patched + text[m.end():]
    applied.append(
        f"folder-search (IW now searches by folder name; "
        f"captured idents lc={lc} q={q} it={it} folder={folder} "
        f"result={result} sort=({sa},{sb}) vscode={vscode} opts={opts})"
    )
    return new_text, applied, skipped


def patch_session_open_column(text: str) -> tuple[str, list[str], list[str]]:
    """Open sessions at far-right tab group instead of creating a split pane.

    Stock: when no existing Claude tab group is found, findUnusedColumn() is
    called which creates a new split pane. Patched: find the rightmost non-empty
    tab group and open there, so the session always lands to the right of
    everything else. Falls back to ViewColumn.Beside only if VS Code has no
    editor groups at all.

    Handles two originals: stock bundle form and the v1 ViewColumn.Active form.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []
    new_text = text

    if SESSION_SPLIT_PATCHED_MARKER in new_text:
        skipped.append("session-open-column (already)")
        return new_text, applied, skipped

    m = SESSION_SPLIT_RE.search(new_text)
    if m:
        # Look forward a few hundred chars for the createWebviewPanel call to
        # capture the right vscode-module identifier (renamed b0 → C0 → f4 → ...).
        tail = new_text[m.end():m.end() + 800]
        mod_match = SESSION_SPLIT_MODULE_RE.search(tail)
        if not mod_match:
            skipped.append("session-open-column (vscode module pattern not found)")
            return new_text, applied, skipped
        module = mod_match.group(1)
        colvar = m.group("colvar")
        replacement = (
            "else{"
            f"let _tg={module}.window.tabGroups.all.filter(Z=>Z.tabs.length>0);"
            f"{colvar}=_tg.length>0"
            "?_tg.reduce((a,c)=>a.viewColumn>c.viewColumn?a:c).viewColumn"
            f":{module}.ViewColumn.Beside"
            "}"
        )
        new_text = new_text[:m.start()] + replacement + new_text[m.end():]
        applied.append("session-open-column (opens at far-right tab group)")
        return new_text, applied, skipped

    if ORIGINAL_SESSION_SPLIT_V1 in new_text:
        # Stale already-patched form lingering on disk — leave a hint, do not rewrite.
        skipped.append("session-open-column (V1 ViewColumn.Active form present)")
        return new_text, applied, skipped

    skipped.append("session-open-column (pattern not found)")
    return new_text, applied, skipped


# --- @ mention dropdown: rank + filter the primary ripgrep search path ------
# findFiles() runs iV0() (ripgrep) as the PRIMARY search; _X() is only the
# catch-block fallback for when ripgrep throws. folder-search and
# folder-sort-priority both live in _X — so on any machine where ripgrep
# works (the normal case) they never run, and the dropdown James sees is
# iV0's raw output. This patch applies the same directory filter and ranking
# to iV0's own return value.
#
# iV0 ends with:
#   return o44(H,z).map(D=>({path:D.path,name:D.filename,
#                            type:D.isDirectory?"directory":"file"}))
# The query z is iV0's first param, so no lookback is needed — cleaner than
# the _X patch. Identifiers are wildcarded so the edit survives re-minification.
RIPGREP_RANK_MARKER = "/*__cce_rgrank__*/"

RIPGREP_RETURN_RE = re.compile(
    r'return (\w{1,4})\((\w{1,4}),(\w{1,4})\)\.map\(\((\w{1,4})\)=>'
    r'\(\{path:\4\.path,name:\4\.filename,'
    r'type:\4\.isDirectory\?"directory":"file"\}\)\)'
)


def patch_ripgrep_folder_rank(text: str) -> tuple[str, list[str], list[str]]:
    """Filter + rank the ripgrep search path's directory results.

    Wraps iV0's `return o44(H,z).map(...)`: directories whose basename does
    not contain the query are dropped ("no match, no show"), and the array
    is sorted by type / match tier / noise / depth / basename — the same
    treatment folder-sort-priority gives the _X fallback, but on the path
    that actually runs when ripgrep works. Files are left untouched.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    if RIPGREP_RANK_MARKER in text:
        skipped.append("ripgrep-folder-rank (already)")
        return text, applied, skipped

    m = RIPGREP_RETURN_RE.search(text)
    if not m:
        skipped.append("ripgrep-folder-rank (iV0 return pattern not found)")
        return text, applied, skipped

    matcher, arr, q, d = m.group(1), m.group(2), m.group(3), m.group(4)
    repl = (
        f'let _ccR={matcher}({arr},{q}).map(({d})=>'
        f'({{path:{d}.path,name:{d}.filename,'
        f'type:{d}.isDirectory?"directory":"file"}}));{RIPGREP_RANK_MARKER}'
        f'let _ccNB=new Set(["node_modules","dist","build","coverage"]);'
        f'let _ccNoisy=p=>p.split("/").some(s=>s&&(s[0]==="."||_ccNB.has(s)));'
        f'let _ccBn=p=>{{let t=p.endsWith("/")?p.slice(0,-1):p;'
        f'return(t.split("/").pop()||"").toLowerCase()}};'
        f'let _ccQ=({q}||"").toLowerCase();'
        f'let _ccRk=n=>!_ccQ?0:n===_ccQ?0:n.startsWith(_ccQ)?1:n.includes(_ccQ)?2:3;'
        f'let _ccKey=o=>{{let n=_ccBn(o.path);return['
        f'o.type==="directory"?0:1,_ccRk(n),_ccNoisy(o.path)?1:0,'
        f'o.path.split("/").length,n]}};'
        f'if(_ccQ)for(let _i=_ccR.length-1;_i>=0;_i--){{'
        f'let _o=_ccR[_i];'
        f'if(_o.type==="directory"&&!_ccBn(_o.path).includes(_ccQ))'
        f'_ccR.splice(_i,1)}}'
        f'_ccR.sort((_x,_y)=>{{let _ka=_ccKey(_x),_kb=_ccKey(_y);'
        f'for(let i=0;i<4;i++)if(_ka[i]!==_kb[i])return _ka[i]-_kb[i];'
        f'return _ka[4].localeCompare(_kb[4])}});'
        f'return _ccR'
    )
    new_text = text[:m.start()] + repl + text[m.end():]
    applied.append(
        f"ripgrep-folder-rank (iV0 output filtered + ranked; "
        f"matcher={matcher} query={q})"
    )
    return new_text, applied, skipped


# --- @ mention: stop ripgrep scanning heavy build / junk trees -------------
# iV0 runs ripgrep with `--files --follow --hidden` and only the excludes it
# can pull from VS Code's search.exclude / files.exclude settings. With
# --hidden it walks into .git, node_modules, and — the real killer — SwiftPM
# .build index stores (mission-control/.build/.../index/store/v5/records/ is
# hundreds of thousands of tiny generated files). That makes every keystroke
# search take 10-15s (results "bounce" as it finishes) and floods the dropdown
# with junk folders. This patch hard-excludes the heavy dirs at the ripgrep
# arg level, so they are never enumerated — upstream of any ranking.
RIPGREP_EXCL_MARKER = "/*__cce_rgexcl__*/"
RIPGREP_ARGS_RE = re.compile(r'let (\w{1,3})=\["--files","--follow","--hidden"\];')
_RG_EXCLUDE_DIRS = [
    ".git", "node_modules", ".build", ".next", "dist",
    "coverage", ".cache", "__pycache__", ".venv", ".turbo",
]


def patch_ripgrep_excludes(text: str) -> tuple[str, list[str], list[str]]:
    """Hard-exclude heavy build / junk directories from the ripgrep scan.

    Injects a `--glob !**/<dir>` for each entry in _RG_EXCLUDE_DIRS right
    after iV0 builds its base arg array, so ripgrep never descends into
    them. Fixes both the multi-second search lag and the junk folders.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    if RIPGREP_EXCL_MARKER in text:
        skipped.append("ripgrep-excludes (already)")
        return text, applied, skipped

    m = RIPGREP_ARGS_RE.search(text)
    if not m:
        skipped.append("ripgrep-excludes (args array not found)")
        return text, applied, skipped

    arr = m.group(1)
    globs = ",".join('"**/' + d + '"' for d in _RG_EXCLUDE_DIRS)
    inject = (
        f'{RIPGREP_EXCL_MARKER}'
        f'for(let _g of [{globs}]){arr}.push("--glob","!"+_g);'
    )
    new_text = text[:m.end()] + inject + text[m.end():]
    applied.append(
        f"ripgrep-excludes ({len(_RG_EXCLUDE_DIRS)} heavy dirs pruned from the scan)"
    )
    return new_text, applied, skipped


# --- @ mention dropdown: filter + rank at findFiles, the confirmed path -----
# Build 67 instrumentation proved findFiles() produces the dropdown — its
# returned array matched the on-screen list exactly. iV0 (ripgrep) throws, so
# the _X fallback supplies results; either way they land in findFiles' local
# L before the [...L,...Z,...O] merge with terminals (Z) and browser tabs (O).
# This filters L (directories whose basename does not contain the query are
# dropped) and sorts it (type / match tier / noise / depth / basename) right
# before the return — so the dropdown is correct regardless of which search
# path ran. Z and O are left untouched.
FFRANK_MARKER = "/*__cce_ffrank__*/"

# Head of findFiles: `findFiles(z){let V=z?.toLowerCase()...` — V is the
# lowercased query, in scope all the way to the return.
FINDFILES_HEAD_RE = re.compile(
    r'findFiles\((\w{1,3})\)\{let (\w{1,3})=\1\?\.toLowerCase\(\)'
)
# findFiles' three-way return: 1=N 2=O 3=Z 4=L 5=B
FINDFILES_RET_RE = re.compile(
    r'if\((\w{1,3})\)return\[\.\.\.(\w{1,3}),\.\.\.(\w{1,3}),\.\.\.(\w{1,3})\];'
    r'else if\((\w{1,3})\)return\[\.\.\.\3,\.\.\.\2,\.\.\.\4\];'
    r'return\[\.\.\.\4,\.\.\.\3,\.\.\.\2\]\}'
)


def patch_findfiles_rank(text: str) -> tuple[str, list[str], list[str]]:
    """Filter + rank the @ mention results at findFiles, the render path.

    Sorts and filters only L (file/folder results) — directories whose
    basename does not contain the query are dropped, then L is ordered by
    type / match tier / noise / depth / basename. Terminals and browser
    tabs in the merge are left alone.

    Returns (new_text, applied_messages, skipped_messages).
    """
    applied: list[str] = []
    skipped: list[str] = []

    if FFRANK_MARKER in text:
        skipped.append("findfiles-rank (already)")
        return text, applied, skipped

    m = FINDFILES_RET_RE.search(text)
    if not m:
        skipped.append("findfiles-rank (findFiles return not found)")
        return text, applied, skipped

    n, o, z, l, b = m.group(1, 2, 3, 4, 5)
    head = FINDFILES_HEAD_RE.search(text, max(0, m.start() - 2000), m.start())
    if not head:
        skipped.append("findfiles-rank (lowercased-query var not found)")
        return text, applied, skipped
    v = head.group(2)

    repl = (
        f'let _ccQ={v}||"";'
        f'let _ccNB=new Set(["node_modules","dist","build","coverage"]);'
        f'let _ccNoisy=p=>!!p&&p.split("/").some(s=>s&&(s[0]==="."||_ccNB.has(s)));'
        f'let _ccBn=p=>{{let t=p||"";t=t.endsWith("/")?t.slice(0,-1):t;'
        f'return(t.split("/").pop()||"").toLowerCase()}};'
        f'let _ccRk=x=>!_ccQ?0:x===_ccQ?0:x.startsWith(_ccQ)?1:x.includes(_ccQ)?2:3;'
        f'let _ccKey=e=>{{let x=_ccBn(e.path);return['
        f'e.type==="directory"?0:1,_ccRk(x),_ccNoisy(e.path)?1:0,'
        f'(e.path||"").split("/").length,x]}};'
        f'if(_ccQ){l}={l}.filter(e=>e.type!=="directory"||_ccBn(e.path).includes(_ccQ));'
        f'{l}.sort((_a,_b)=>{{let _ka=_ccKey(_a),_kb=_ccKey(_b);'
        f'for(let i=0;i<4;i++)if(_ka[i]!==_kb[i])return _ka[i]-_kb[i];'
        f'return _ka[4].localeCompare(_kb[4])}});{FFRANK_MARKER}'
        f'return {n}?[...{o},...{z},...{l}]:'
        f'{b}?[...{z},...{o},...{l}]:[...{l},...{z},...{o}]}}'
    )
    new_text = text[:m.start()] + repl + text[m.end():]
    applied.append(
        f"findfiles-rank (L filtered + ranked at findFiles return; query var={v})"
    )
    return new_text, applied, skipped


def patch_extension_js(ext_dir: Path) -> str:
    """Patch the Node.js-side extension.js (not the webview bundle)."""
    js = ext_dir / "extension.js"
    if not js.exists():
        return f"ext-js SKIP (no extension.js): {js}"
    text = js.read_text()
    applied: list[str] = []
    skipped: list[str] = []

    text, db_applied, db_skipped = patch_dp4_basename(text)
    applied.extend(db_applied)
    skipped.extend(db_skipped)

    text, fs_applied, fs_skipped = patch_folder_search(text)
    applied.extend(fs_applied)
    skipped.extend(fs_skipped)

    text, sp_applied, sp_skipped = patch_folder_sort_priority(text)
    applied.extend(sp_applied)
    skipped.extend(sp_skipped)

    text, rg_applied, rg_skipped = patch_ripgrep_folder_rank(text)
    applied.extend(rg_applied)
    skipped.extend(rg_skipped)

    text, rgx_applied, rgx_skipped = patch_ripgrep_excludes(text)
    applied.extend(rgx_applied)
    skipped.extend(rgx_skipped)

    text, ff_applied, ff_skipped = patch_findfiles_rank(text)
    applied.extend(ff_applied)
    skipped.extend(ff_skipped)

    text, soc_applied, soc_skipped = patch_session_open_column(text)
    applied.extend(soc_applied)
    skipped.extend(soc_skipped)

    text, sme_applied, sme_skipped = patch_strip_broken_move_editor(text)
    applied.extend(sme_applied)
    skipped.extend(sme_skipped)

    # --- Structural sanity check: warn when an applied patch may have
    # silently regressed because Anthropic restructured surrounding code.
    # The producer IIFE assumes K is the sessionId in setupPanel(V,K,B,x),
    # which we verify by checking that the bundle still contains the call
    # site `if(this.setupPanel(<U>,<V>,<K>,<q>),<V>) this.sessionPanels.set(<V>,<U>)`
    # — the outer V here is what gets passed in as the K parameter, and is
    # also what gets stored as the sessionPanels key. If that pattern goes
    # missing, K is no longer reliably the sessionId and the V.title= write
    # in the IIFE will quietly stop working even though the patch "applied".
    warnings: list[str] = []
    if "V.title=_o[K]" in text:
        # Match the call-site shape, allowing the minifier to rename the vars.
        # Use a raw regex so `\(` is interpreted as a literal paren.
        if not re.search(
            r"setupPanel\([A-Za-z0-9_\$]+,([A-Za-z0-9_\$]+),"
            r"[A-Za-z0-9_\$]+,[A-Za-z0-9_\$]+\),\1\)"
            r"this\.sessionPanels\.set\(\1,",
            text,
        ):
            warnings.append(
                "overrides-producer-verify (setupPanel→sessionPanels.set "
                "pattern not found — K may no longer be the sessionId; "
                "editor tab live-update may have silently regressed)"
            )

    if not applied:
        if all("already" in s for s in skipped) and not warnings:
            return f"ext-js already patched: {js.name}"
        if warnings and not applied:
            return f"ext-js WARN ({'; '.join(warnings)}): {js.name}"
        return f"ext-js SKIP ({'; '.join(skipped)}): {js.name}"

    backup = js.with_suffix(f".js.bak.{int(time.time())}")
    shutil.copy(js, backup)
    js.write_text(text)
    msg = f"ext-js patched: {js.name} ({', '.join(applied)}"
    if skipped:
        msg += f"; skipped: {', '.join(skipped)}"
    if warnings:
        msg += f"; WARN: {', '.join(warnings)}"
    msg += f", backup: {backup.name})"
    return msg


def main() -> int:
    ext_dirs = find_extension_dirs()
    if not ext_dirs:
        print("no Claude Code VS Code extension found in known locations", file=sys.stderr)
        return 1
    rc = 0
    for d in ext_dirs:
        for fn in (patch_css, patch_js, patch_extension_js):
            try:
                print(fn(d))
            except Exception as e:
                print(f"FAIL {fn.__name__} {d}: {e}", file=sys.stderr)
                rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
