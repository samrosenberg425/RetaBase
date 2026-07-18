// Minimal Playwright config for the RetaBase E2E suite.
module.exports = {
  testDir: '.',
  timeout: 30000,
  expect: { timeout: 5000 },
  use: { headless: true },
  reporter: 'list',
};
