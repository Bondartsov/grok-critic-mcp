#!/usr/bin/env node
/*
 * align-md-tables.mjs — выравнивание GFM-таблиц в Markdown (глобальный инструмент).
 *
 * Считает ВИЗУАЛЬНУЮ ширину (display width), а не length:
 *   кириллица/латиница/цифры = 1, CJK/emoji = 2, combining/variation-selector = 0.
 * Вне ```/~~~ code-fence форматирует GFM-таблицы. Разделитель пишет ОБЫЧНЫМИ
 * дефисами (----), без ведущего двоеточия для left/default.
 * Осознанные center (:-:) и right (--:) сохраняются.
 * ВНУТРИ fence таблицы не трогаются, но box-арт (┌─┐│└┘) ПРОВЕРЯЕТСЯ на
 * выравнивание: прогоны строк, начинающихся и заканчивающихся псевдографикой,
 * должны иметь одинаковую display-width. Авто-правки нет — только диагностика
 * (--check → exit 1; формат-режим и hook → предупреждение в stderr).
 *
 * CLI:
 *   node align-md-tables.mjs <file.md> [file2.md ...]   # форматировать на месте
 *   node align-md-tables.mjs --check <file.md>           # только проверить (exit 1 если криво)
 *
 * Модуль:
 *   import { formatFile, processMarkdown } from '.../align-md-tables.mjs'
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

/** Ширина одного кодпоинта в моноширинных ячейках. */
function cpWidth(cp) {
  if (
    cp === 0x200b || cp === 0x200c || cp === 0x200d || cp === 0xfeff ||
    (cp >= 0x0300 && cp <= 0x036f) ||
    (cp >= 0x1ab0 && cp <= 0x1aff) ||
    (cp >= 0x20d0 && cp <= 0x20ff) ||
    (cp >= 0xfe00 && cp <= 0xfe0f)
  ) return 0;
  if (
    (cp >= 0x1100 && cp <= 0x115f) ||
    (cp >= 0x2600 && cp <= 0x27bf) ||
    (cp >= 0x2b00 && cp <= 0x2bff) ||
    (cp >= 0x2e80 && cp <= 0x303e) ||
    (cp >= 0x3041 && cp <= 0x33ff) ||
    (cp >= 0x3400 && cp <= 0x4dbf) ||
    (cp >= 0x4e00 && cp <= 0x9fff) ||
    (cp >= 0xa000 && cp <= 0xa4cf) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe30 && cp <= 0xfe4f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f000 && cp <= 0x1faff) ||
    (cp >= 0x20000 && cp <= 0x3fffd)
  ) return 2;
  return 1;
}

/** Визуальная ширина строки (итерация по кодпоинтам). */
function dispWidth(s) {
  let w = 0;
  for (const ch of s) w += cpWidth(ch.codePointAt(0));
  return w;
}

/** Дополнить ячейку пробелами до width с учётом выравнивания. */
function padCell(cell, width, align) {
  const pad = width - dispWidth(cell);
  if (pad <= 0) return cell;
  if (align === 'right') return ' '.repeat(pad) + cell;
  if (align === 'center') {
    const l = pad >> 1;
    return ' '.repeat(l) + cell + ' '.repeat(pad - l);
  }
  return cell + ' '.repeat(pad);
}

/** Разбить строку-ряд на ячейки (учитывает экранированные \|). */
function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split(/(?<!\\)\|/).map((c) => c.trim());
}

const SEPARATOR = /^\s*\|?(\s*:?-{1,}:?\s*\|)+\s*:?-{1,}:?\s*\|?\s*$/;
const isRow = (l) => l.includes('|') && l.trim() !== '';
const isSeparator = (l) => SEPARATOR.test(l) && l.includes('-');

function alignOf(sepCell) {
  const c = sepCell.trim();
  const L = c.startsWith(':'), R = c.endsWith(':');
  if (L && R) return 'center';
  if (R) return 'right';
  return 'left'; // явный :--- трактуем как left → нормализуется в ----
}

function formatBlock(lines) {
  const indent = lines[0].match(/^(\s*)/)[1];
  const header = splitRow(lines[0]);
  const aligns = splitRow(lines[1]).map(alignOf);
  const bodyRows = lines.slice(2).map(splitRow);
  const nCols = Math.max(header.length, aligns.length, ...bodyRows.map((r) => r.length));

  const norm = (r) => Array.from({ length: nCols }, (_, i) => r[i] ?? '');
  const allCells = [header, ...bodyRows].map(norm);
  const widths = Array.from({ length: nCols }, (_, c) =>
    Math.max(3, ...allCells.map((r) => dispWidth(r[c]))),
  );
  const al = (c) => aligns[c] ?? 'left';

  const renderRow = (cells) =>
    indent + '| ' + norm(cells).map((cell, c) => padCell(cell, widths[c], al(c))).join(' | ') + ' |';

  const renderSep = () =>
    indent + '| ' + widths.map((w, c) => {
      const a = al(c);
      if (a === 'center') return ':' + '-'.repeat(Math.max(1, w - 2)) + ':';
      if (a === 'right') return '-'.repeat(Math.max(1, w - 1)) + ':';
      return '-'.repeat(w); // left/default → без двоеточия
    }).join(' | ') + ' |';

  return [renderRow(header), renderSep(), ...bodyRows.map(renderRow)];
}

/** Отформатировать markdown-текст; вернуть { text, tables }. */
export function processMarkdown(src) {
  const lines = src.split('\n');
  const out = [];
  let inFence = false, fenceMark = '';
  let tables = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*(```+|~~~+)/);
    if (fence) {
      if (!inFence) { inFence = true; fenceMark = fence[1][0]; }
      else if (line.includes(fenceMark)) { inFence = false; }
      out.push(line);
      continue;
    }
    if (!inFence && isRow(line) && i + 1 < lines.length && isSeparator(lines[i + 1])) {
      const block = [line, lines[i + 1]];
      let j = i + 2;
      while (j < lines.length && isRow(lines[j]) && !isSeparator(lines[j])) {
        block.push(lines[j]); j++;
      }
      out.push(...formatBlock(block));
      tables++;
      i = j - 1;
      continue;
    }
    out.push(line);
  }
  return { text: out.join('\n'), tables };
}

// --- Box-арт в code-fence: только проверка выравнивания, без авто-правки ---

// Псевдографика Unicode; ASCII '|' сознательно НЕ сюда — иначе ложно сработает
// на примерах markdown-таблиц внутри fence.
const BOX_START = /^[│┌└├┬┼╔╠╚╟╭]/;
const BOX_END = /[│┐┘┤┴┼╗╣╝╢╮]$/;

/**
 * Найти прогоны box-арта в code-fence с расходящейся шириной.
 * Прогон = ≥3 подряд непустых строк fence-блока, каждая начинается
 * псевдографикой и заканчивается ею (trailing whitespace не считается).
 * Деревья (├── … # комментарий) и prose отсеиваются сами: их строки
 * заканчиваются текстом. Вернуть [{ from, to, widths }] (1-based строки).
 */
export function scanBoxArt(lines) {
  const issues = [];
  let inFence = false, fenceMark = '';
  let run = [], runStart = -1;

  const flush = () => {
    if (run.length >= 3) {
      const widths = [...new Set(run.map(dispWidth))].sort((a, b) => a - b);
      if (widths.length > 1) {
        issues.push({ from: runStart + 1, to: runStart + run.length, widths });
      }
    }
    run = []; runStart = -1;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*(```+|~~~+)/);
    if (fence) {
      flush();
      if (!inFence) { inFence = true; fenceMark = fence[1][0]; }
      else if (line.includes(fenceMark)) { inFence = false; }
      continue;
    }
    if (!inFence) continue;
    const t = line.replace(/\s+$/, '');
    if (BOX_START.test(t) && BOX_END.test(t)) {
      if (run.length === 0) runStart = i;
      run.push(t);
    } else {
      flush();
    }
  }
  flush();
  return issues;
}

/** Отформатировать файл на месте. Вернуть { changed, tables, boxes }. */
export function formatFile(file, { check = false } = {}) {
  const src = readFileSync(file, 'utf8');
  const { text, tables } = processMarkdown(src);
  const boxes = scanBoxArt(src.split('\n'));
  const changed = text !== src;
  if (changed && !check) writeFileSync(file, text, 'utf8');
  return { changed, tables, boxes };
}

// --- CLI (только при прямом запуске, не при import из hook) ---
const isCLI = process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (isCLI) {
  const args = process.argv.slice(2);
  const check = args.includes('--check');
  const files = args.filter((a) => !a.startsWith('--'));
  if (files.length === 0) {
    console.error('Укажите хотя бы один .md файл');
    process.exit(2);
  }
  let changed = 0;
  let boxIssues = 0;
  for (const f of files) {
    const r = formatFile(f, { check });
    if (r.changed) changed++;
    console.log(`${r.changed ? (check ? '≠' : '✔ выровнен') : '= без изменений'}  ${f}  (таблиц: ${r.tables})`);
    for (const b of r.boxes) {
      boxIssues++;
      console.log(`  ⚠ box-арт не выровнен: строки ${b.from}–${b.to}, ширины ${b.widths.join('/')} — авто-правки нет, выровняйте вручную`);
    }
  }
  if (check && (changed > 0 || boxIssues > 0)) process.exit(1);
}
