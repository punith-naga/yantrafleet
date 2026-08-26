// Extracts every inline <script> from console/index.html and syntax-checks it.
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
const html = readFileSync(new URL('../console/index.html', import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('no inline scripts found'); process.exit(1); }
writeFileSync('/tmp/_console_check.js', scripts.join('\n'));
execFileSync('node', ['--check', '/tmp/_console_check.js'], { stdio: 'inherit' });
console.log(`console OK — ${scripts.length} script block(s) parse clean`);
