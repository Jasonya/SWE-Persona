// This browser fixture uses only the benchmark image's installed Playwright.
const fs = require('node:fs');
const http = require('node:http');
const crypto = require('node:crypto');
const { chromium } = require('/testbed/node_modules/@playwright/test');
const content = ['Opening hours: 09:00 to 17:00.', 'Collection point: front counter.', 'Contact: ask the shop owner in person.'];
const html = `<!doctype html><meta charset="utf-8"><title>Controlled shop help preview</title>
<link rel="stylesheet" href="/core.css"><style>section.cover{height:100vh;min-height:720px}.markdown-section{min-height:100vh}</style>
<div id="app"></div><script>window.$docsify={coverpage:true,loadSidebar:false,name:'Shop help preview'};</script>
<script src="/docsify.js"></script>`;
const assets = {'/': ['text/html', html], '/index.html': ['text/html', html],
  '/README.md': ['text/plain', '# Quick start\n\n' + content.join('\n\n') + '\n'],
  '/_coverpage.md': ['text/plain', '# Quick start\n\nWelcome to the controlled shop help preview.\n'],
  '/docsify.js': ['text/javascript', fs.readFileSync('/testbed/dist/docsify.js')],
  '/core.css': ['text/css', fs.readFileSync('/testbed/dist/themes/core.css')]};
const server = http.createServer((req, res) => {
  const file = assets[new URL(req.url, 'http://localhost').pathname];
  res.writeHead(file ? 200 : 404, {'Content-Type': file ? file[0] : 'text/plain'});
  res.end(file ? file[1] : 'No such fixture asset');
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  const url = `http://127.0.0.1:${server.address().port}/`;
  const records = [];
  const modes = process.argv.includes('--calibration') ? ['candidate', 'controlled_main', 'controlled_cover', 'controlled_data_loss'] : ['candidate'];
  try {
    for (const mode of modes) {
      const page = await browser.newPage({viewport: {width: 1000, height: 720}});
      const requests = [];
      await page.route('**/*', route => {
        const target = new URL(route.request().url());
        if (target.origin === new URL(url).origin) return route.continue();
        requests.push(route.request().url());
        return route.abort();
      });
      const steps = [];
      for (const action of ['click_cover_title', 'direct_fragment']) {
        await page.goto(url + (action === 'direct_fragment' ? '#/?id=quick-start' : ''), {waitUntil: 'networkidle'});
        await page.locator('#main h1').waitFor();
        await page.locator('section.cover h1').waitFor();
        if (action === 'click_cover_title') await page.locator('section.cover h1 a').click();
        await page.waitForTimeout(300);
        // These controls exercise the measurement oracle, not a repaired
        // Docsify build. Their interventions are explicit in every receipt.
        if (mode !== 'candidate') {
          await page.locator(mode === 'controlled_cover' ? 'section.cover h1' : '#main h1').evaluate(el => el.scrollIntoView());
          if (mode === 'controlled_data_loss') await page.locator('#main p').first().evaluate(el => el.remove());
        }
        const observed = await page.evaluate(() => ({
          scroll_y: window.scrollY,
          main_top: document.querySelector('#main h1').getBoundingClientRect().top,
          cover_top: document.querySelector('section.cover h1').getBoundingClientRect().top,
          paragraphs: [...document.querySelectorAll('#main p')].map(p => p.textContent.trim()),
          headings: [...document.querySelectorAll('h1')].map(p => p.textContent.trim())
        }));
        steps.push({action, ...observed, navigation_pass: Math.abs(observed.main_top) < 120,
          content_pass: JSON.stringify(observed.paragraphs) === JSON.stringify(content)});
      }
      records.push({name: mode, intervention: mode === 'candidate' ? 'none' : 'oracle calibration only: ' + mode,
        expected_content: content, steps, external_requests: requests, tool: 'existing headless Chromium',
        user_operations: ['Open the preview in the existing browser', 'Click Quick start', 'Read the unchanged help content']});
      await page.close();
    }
    process.stdout.write(JSON.stringify({browser_version: browser.version(), records,
      assets: Object.entries(assets).map(([path, [, bytes]]) => ({path, sha256: crypto.createHash('sha256').update(bytes).digest('hex')})),
      owner_installations: [], owner_build_commands: [], production_changes: []}));
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error.stack); server.close(); process.exitCode = 1; });
