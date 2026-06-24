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

async function addToRuntimeBaseClasses() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showErrorMessage('No active editor.');
    return;
  }

  // Determine class name from selection, class definition line, or word at cursor
  let className = '';
  const sel = editor.selection;
  if (!sel.isEmpty) {
    className = editor.document.getText(sel).trim();
  } else {
    const lineText = editor.document.lineAt(sel.active.line).text;
    const classMatch = /^\s*class\s+(\w+)/.exec(lineText);
    if (classMatch) {
      className = classMatch[1];
    } else {
      const wordRange = editor.document.getWordRangeAtPosition(sel.active);
      if (wordRange) {
        className = editor.document.getText(wordRange);
      }
    }
  }

  // Determine module path from file path relative to src/
  const filePath = editor.document.uri.fsPath;
  const wsFolders = vscode.workspace.workspaceFolders;
  if (!wsFolders || wsFolders.length === 0) {
    vscode.window.showErrorMessage('No workspace folder open.');
    return;
  }
  const wsRoot = wsFolders[0].uri.fsPath;
  const srcRoot = path.join(wsRoot, 'src');

  let modulePath = '';
  if (filePath.toLowerCase().startsWith(srcRoot.toLowerCase() + path.sep)) {
    const relative = path.relative(srcRoot, filePath);
    modulePath = relative.replace(/\\/g, '.').replace(/\//g, '.').replace(/\.py$/, '');
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