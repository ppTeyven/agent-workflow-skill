#!/usr/bin/env node
/**
 * check_syntax.mjs —— 校验 HTML 文件里内联 <script type="module"> 的语法。
 *
 * 用法:
 *   node check_syntax.mjs <file.html> [more.html ...]
 *   node check_syntax.mjs --list <file.html>      # 只列出找到的 script
 *
 * 为什么需要:
 *   改压缩产物 / 单文件 HTML 后，"页面白屏、模块没跑起来"经常是内联 ES module
 *   语法错误导致（一个 TypeError 会静默打断整个 IIFE）。文本编辑器不报错，
 *   浏览器也不报在明显的地方，所以需要一道独立门禁。
 *
 * 实现:
 *   1. 用正则抽出 <script type="module"> ... </script> 的内容；
 *   2. 逐个做纯语法解析（**不求值**，所以 import 的目标模块不必真实存在）。
 *   3. 解析优先用 node --experimental-vm-modules 的 vm.SourceTextModule；
 *      没有该 flag 时自动回退到 `node --check` 临时文件。
 *
 * 退出码: 0 = 全部通过；1 = 有语法错误；2 = 参数/文件问题。
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const MODULE_RE = /<script\b([^>]*)\btype\s*=\s*["']module["']([^>]*)>([\s\S]*?)<\/script\s*>/gi;
const ANY_SCRIPT_RE = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;

const HAS_SOURCE_TEXT_MODULE = typeof vm.SourceTextModule === 'function';

/** 收集一个 HTML 里的所有内联 script */
export function extractAllInlineScripts(html) {
  const out = [];
  let m;
  ANY_SCRIPT_RE.lastIndex = 0;
  while ((m = ANY_SCRIPT_RE.exec(html)) !== null) {
    const attrs = m[1] || '';
    const isModule = /\btype\s*=\s*["']module["']/i.test(attrs);
    out.push({ attrs, isModule, code: m[2] || '' });
  }
  return out;
}

/** 收集所有 type="module" 的内联 script */
export function extractModuleScripts(html) {
  return extractAllInlineScripts(html).filter((s) => s.isModule);
}

/** 用 node --check 校验一段代码（回退路径） */
function checkViaNodeCheck(code, filename) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chk-'));
  const file = path.join(dir, filename.replace(/[^A-Za-z0-9._-]/g, '_'));
  try {
    fs.writeFileSync(file, code, 'utf8');
    const r = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' });
    if (r.status === 0) return { ok: true, error: null };
    const msg = (r.stderr || r.stdout || 'syntax error').trim();
    // 只保留报错主体，去掉 node --check 的堆栈尾巴（"at ..." 行 / 源码引用行）
    const lines = msg.split(/\r?\n/);
    const cut = lines.findIndex((l) => /^\s+at\s/.test(l));
    const body = (cut === -1 ? lines : lines.slice(0, cut)).map((l) => l.split(file).join('<module>'));
    return { ok: false, error: body.join('\n').trim() };
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
}

/**
 * 语法检查：只解析不求值。
 * 返回 { ok: boolean, error: string|null }
 */
export function checkSyntax(code, filename = 'inline-module.mjs') {
  if (HAS_SOURCE_TEXT_MODULE) {
    try {
      // SourceTextModule 构造即触发解析；不 link() 就不会去解析 import 的目标。
      new vm.SourceTextModule(code, { identifier: filename });
      return { ok: true, error: null };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  }
  return checkViaNodeCheck(code, filename);
}

function checkFile(file, listOnly) {
  if (!fs.existsSync(file)) {
    console.error(`x 文件不存在: ${file}`);
    return 1;
  }
  const raw = fs.readFileSync(file);
  // Windows 上带 BOM 的文件很常见（UTF-8 BOM / UTF-16）。不处理会让正则全部失配，
  // 表现为「文件里明明有 <script>，却报告 0 个内联 script」。
  let html = raw.toString('utf8');
  if (html.charCodeAt(0) === 0xfeff) html = html.slice(1);
  if (html.charCodeAt(0) === 0xff && html.charCodeAt(1) === 0xfe) {
    console.error(`x ${file}: 是 UTF-16LE 编码，请先转成 UTF-8`);
    return 1;
  }
  if (html.charCodeAt(0) === 0xfe && html.charCodeAt(1) === 0xff) {
    console.error(`x ${file}: 是 UTF-16BE 编码，请先转成 UTF-8`);
    return 1;
  }
  const all = extractAllInlineScripts(html);
  const mods = all.filter((s) => s.isModule);

  if (listOnly) {
    console.log(`${file}: 共 ${all.length} 个内联 script，其中 ${mods.length} 个 type="module"`);
    all.forEach((s, i) => {
      console.log(`  [${i}] ${s.isModule ? 'module' : 'classic'}  ${s.code.length} 字符  ${s.attrs.trim().slice(0, 80)}`);
    });
    return 0;
  }

  if (!mods.length) {
    console.log(`+ ${file}: 没有内联 type="module"（跳过）`);
    return 0;
  }

  let failed = 0;
  mods.forEach((s, i) => {
    const r = checkSyntax(s.code, `${path.basename(file)}-module-${i}.mjs`);
    if (r.ok) {
      console.log(`  + module #${i} (${s.code.length} 字符) 语法通过`);
    } else {
      failed++;
      console.error(`  x module #${i} (${s.code.length} 字符) 语法错误: ${r.error}`);
    }
  });

  if (failed) {
    console.error(`x ${file}: ${failed}/${mods.length} 个 module script 语法错误`);
    return 1;
  }
  console.log(`+ ${file}: ${mods.length} 个 module script 语法全部通过`);
  return 0;
}

function main(argv) {
  const args = argv.slice(2);
  if (!args.length) {
    console.error('用法: node check_syntax.mjs <file.html> [more.html ...]');
    console.error('      node check_syntax.mjs --list <file.html>');
    return 2;
  }
  const listOnly = args.includes('--list');
  const files = args.filter((a) => !a.startsWith('--'));
  if (!files.length) {
    console.error('没有指定文件');
    return 2;
  }
  let rc = 0;
  for (const f of files) {
    const r = checkFile(f, listOnly);
    if (r !== 0) rc = r;
  }
  return rc;
}

// 直接运行时执行；被 import 时不执行。
const invokedDirectly = process.argv[1]
  && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (invokedDirectly) {
  process.exit(main(process.argv));
}
