// After a build: record a hash of the sources in static/build.json. tests/test_web.py recomputes it, so a commit
// whose bundle is older than ui/ fails with "run npm run build".
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'

const here = import.meta.dirname
const walk = (dir) => readdirSync(dir).flatMap((n) => {
  const p = join(dir, n)
  return statSync(p).isDirectory() ? walk(p) : [p]
})
const files = [...walk(join(here, 'src')), ...['index.html', 'package.json', 'vite.config.ts'].map((n) => join(here, n))]
  .map((p) => relative(here, p).split('\\').join('/')).sort()
const h = createHash('sha256')
for (const f of files) h.update(f).update('\0').update(readFileSync(join(here, f))).update('\0')
writeFileSync(join(here, '../src/shortbot/web/static/build.json'), JSON.stringify({ src_hash: h.digest('hex'), files: files.length }) + '\n')
