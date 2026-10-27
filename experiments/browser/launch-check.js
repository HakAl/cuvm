// Image build check (docs/image.md, Derived browser image): headed WebKit through the build proxy
// from an ssh session, so first-launch dialogs show up before the image is recorded.
const { webkit } = require('/opt/homebrew/lib/node_modules/@playwright/mcp/node_modules/playwright');
(async () => {
  const b = await webkit.launch({ headless: false, proxy: { server: process.env.HTTPS_PROXY } });
  const p = await b.newPage();
  try {
    const r = await p.goto(process.argv[2] || 'https://registry.npmjs.org/', { timeout: 30000 });
    console.log('status', r && r.status());
  } catch (e) { console.log('goto error', e.message.split('\n')[0]); }
  await new Promise(r => setTimeout(r, 3000));
  await b.close();
})().catch(e => { console.log('launch error', e.message.split('\n')[0]); process.exit(1); });
