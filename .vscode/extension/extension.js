'use strict';

const vscode = require('vscode');
const path = require('path');
const fs = require('fs');

/**
 * Inserts fqcn into the runtime-evaluated-base-classes array in pyproject.toml content.
 * Returns the updated content string, or null if the array was not found.
 * Uses a character-by-character depth counter to reliably find the closing bracket.
 */
function insertIntoRuntimeBaseClasses(content, fqcn) {
  const lines = content.split('\n');
  let inArray = false;
  let depth = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (!inArray) {
      if (/^\s*runtime-evaluated-base-classes\s*=\s*\[/.test(line)) {
        inArray = true;
        depth = 0;
        for (const ch of line) {
          if (ch === '[') depth++;
          else if (ch === ']') depth--;
        }
        if (depth <= 0) {
          // Inline array — not supported
          return null;
        }
      }
    } else {
      for (const ch of line) {
        if (ch === '[') depth++;
        else if (ch === ']') depth--;
      }
      if (depth <= 0) {
        // This line closes the array. Detect entry indentation from previous entry lines.
        let entryIndent = '      ';
        for (let j = i - 1; j >= 0; j--) {
          const m = /^(\s+)"/.exec(lines[j]);
          if (m) { entryIndent = m[1]; break; }
        }
        lines.splice(i, 0, `${entryIndent}"${fqcn}",`);
        return lines.join('\n');
      }
    }
  }
  return null;
}

/**
 * Ensures a multi-line runtime-evaluated-base-classes array exists in the content.
 * If the key is missing it is added as an empty multi-line array under the
 * [tool.ruff.lint.flake8-type-checking] table, creating that table if needed.
 * Returns the updated content (unchanged if the key already exists).
 */
function ensureRuntimeBaseClassesArray(content) {
  if (/^\s*runtime-evaluated-base-classes\s*=\s*\[/m.test(content)) {
    return content;
  }

  const emptyArray = ['  runtime-evaluated-base-classes = [', '  ]'];
  const lines = content.split('\n');

  const headerIdx = lines.findIndex(l =>
    /^\s*\[tool\.ruff\.lint\.flake8-type-checking\]\s*$/.test(l));

  if (headerIdx !== -1) {
    lines.splice(headerIdx + 1, 0, ...emptyArray);
    return lines.join('\n');
  }

  // Table not present — append a new section to the end of the file.
  const trimmed = content.replace(/\s*$/, '');
  return `${trimmed}\n\n[tool.ruff.lint.flake8-type-checking]\n${emptyArray.join('\n')}\n`;
}

/**
 * Walks up the directory tree from a Python file, following packages that contain an
 * __init__.py(i), to build the file's fully-qualified module path. Works for both
 * workspace `src/` layouts and installed packages under site-packages or typeshed.
 */
function computeModulePath(filePath) {
  if (!filePath) return '';
  const hasInit = dir =>
    fs.existsSync(path.join(dir, '__init__.py')) ||
    fs.existsSync(path.join(dir, '__init__.pyi'));

  const fileBase = path.basename(filePath).replace(/\.pyi?$/i, '');
  const chain = [];
  if (fileBase && fileBase !== '__init__') {
    chain.push(fileBase);
  }

  let dir = path.dirname(filePath);
  while (dir && hasInit(dir)) {
    chain.push(path.basename(dir));
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }

  return chain.reverse().join('.');
}

/**
 * Follows the language server's definition provider from a starting position until it
 * reaches the original `class`/`def` definition, tracing through re-exports and imports
 * (including into installed library code). Returns { fsPath, className } or null.
 */
async function resolveDefinition(startUri, startPos) {
  const normalize = item => {
    if (!item) return null;
    if (item.targetUri) {
      return { uri: item.targetUri, range: item.targetSelectionRange || item.targetRange };
    }
    if (item.uri && item.range) {
      return { uri: item.uri, range: item.range };
    }
    return null;
  };

  let curUri = startUri;
  let curPos = startPos;
  let result = null;
  const visited = new Set();

  for (let i = 0; i < 16; i++) {
    let defs;
    try {
      defs = await vscode.commands.executeCommand(
        'vscode.executeDefinitionProvider', curUri, curPos);
    } catch {
      break;
    }
    if (!defs || defs.length === 0) break;

    const loc = normalize(defs[0]);
    if (!loc) break;

    let doc;
    try {
      doc = await vscode.workspace.openTextDocument(loc.uri);
    } catch {
      break;
    }

    const lineText = doc.lineAt(loc.range.start.line).text;
    result = { fsPath: loc.uri.fsPath };

    const defMatch = /^\s*(?:class|def)\s+(\w+)/.exec(lineText);
    if (defMatch) {
      result.className = defMatch[1];
      break;
    }

    const key = `${loc.uri.toString()}:${loc.range.start.line}:${loc.range.start.character}`;
    if (visited.has(key)) break;
    visited.add(key);

    curUri = loc.uri;
    curPos = loc.range.start;
  }

  return result;
}

async function addToRuntimeBaseClasses() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showErrorMessage('No active editor.');
    return;
  }

  // Determine the identifier name and a position to resolve from selection or cursor.
  const sel = editor.selection;
  let position = sel.active;
  let fallbackName = '';
  if (!sel.isEmpty) {
    position = sel.start;
    fallbackName = editor.document.getText(sel).trim();
    const wordRange = editor.document.getWordRangeAtPosition(sel.start);
    if (wordRange) {
      position = wordRange.start;
      fallbackName = editor.document.getText(wordRange);
    }
  } else {
    const lineText = editor.document.lineAt(sel.active.line).text;
    const classMatch = /^\s*class\s+(\w+)/.exec(lineText);
    if (classMatch) {
      fallbackName = classMatch[1];
    } else {
      const wordRange = editor.document.getWordRangeAtPosition(sel.active);
      if (wordRange) {
        fallbackName = editor.document.getText(wordRange);
      }
    }
  }

  const wsFolders = vscode.workspace.workspaceFolders;
  if (!wsFolders || wsFolders.length === 0) {
    vscode.window.showErrorMessage('No workspace folder open.');
    return;
  }
  const wsRoot = wsFolders[0].uri.fsPath;

  // Resolve the symbol's original definition by following the language server's
  // definition provider into imported modules (including installed libraries).
  const resolved = await resolveDefinition(editor.document.uri, position);

  let className = fallbackName;
  let modulePath = '';
  if (resolved) {
    if (resolved.className) className = resolved.className;
    modulePath = computeModulePath(resolved.fsPath);
  }
  // Fall back to the current file's module path if resolution failed.
  if (!modulePath) {
    modulePath = computeModulePath(editor.document.uri.fsPath);
  }

  const suggested = modulePath && className ? `${modulePath}.${className}` : className;

  // Prompt user to confirm / edit the fully-qualified class name
  const fqcn = await vscode.window.showInputBox({
    title: 'Add to runtime-evaluated-base-classes',
    prompt: 'Fully-qualified class name to add to pyproject.toml',
    value: suggested,
    validateInput: v => (v && v.trim()) ? null : 'Cannot be empty',
  });
  if (!fqcn) return;

  // Locate pyproject.toml in workspace root
  const pyprojectPath = path.join(wsRoot, 'pyproject.toml');
  if (!fs.existsSync(pyprojectPath)) {
    vscode.window.showErrorMessage('pyproject.toml not found in workspace root.');
    return;
  }

  const original = fs.readFileSync(pyprojectPath, 'utf8');

  if (original.includes(`"${fqcn.trim()}"`)) {
    vscode.window.showInformationMessage(`"${fqcn}" is already listed in runtime-evaluated-base-classes.`);
    return;
  }

  const ensured = ensureRuntimeBaseClassesArray(original);
  const updated = insertIntoRuntimeBaseClasses(ensured, fqcn.trim());
  if (updated === null) {
    vscode.window.showErrorMessage('Could not locate runtime-evaluated-base-classes array in pyproject.toml.');
    return;
  }

  fs.writeFileSync(pyprojectPath, updated, 'utf8');
  vscode.window.showInformationMessage(`Added "${fqcn}" to runtime-evaluated-base-classes in pyproject.toml.`);
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('drekker.addToRuntimeBaseClasses', addToRuntimeBaseClasses),
  );
}

function deactivate() {}

module.exports = { activate, deactivate };