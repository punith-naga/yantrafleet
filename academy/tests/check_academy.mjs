// Extracts every inline <script> from academy/index.html and syntax-checks it.
// Mirrors scripts/check_console.mjs. Run:  node academy/tests/check_academy.mjs
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('no inline scripts found'); process.exit(1); }
if (scripts.length !== 1) { console.error(`academy must keep exactly ONE inline script block (found ${scripts.length})`); process.exit(1); }
const out = join(tmpdir(), '_academy_check.js');
writeFileSync(out, scripts.join('\n'));
execFileSync('node', ['--check', out], { stdio: 'inherit' });
console.log(`academy OK — ${scripts.length} script block(s) parse clean`);
