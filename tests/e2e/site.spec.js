// Real-browser E2E tests for the RetaBase dashboard. Unlike the Python suite
// (which asserts substrings in the generated HTML), these actually EXECUTE the
// site's JavaScript in Chromium and verify behavior: rendering, search filtering,
// card -> detail dialog, keyboard operation, and tab switching.
//
// Run:  python3 make_fixture.py && npx playwright test   (from tests/e2e/)
const { test, expect } = require('@playwright/test');
const path = require('path');

const url = 'file://' + path.resolve(__dirname, 'fixture.html');

// The site now lands on the Home tab; the evidence feed lives behind the Evidence
// tab. Navigate there before asserting on cards.
async function gotoEvidence(page) {
  await page.goto(url);
  await page.click('#tab-evidence');
  await page.locator('.card').first().waitFor();
}

test('renders evidence cards after opening Evidence', async ({ page }) => {
  await page.goto(url);
  await page.click('#tab-evidence');
  await expect(page.locator('.card').first()).toBeVisible();
  expect(await page.locator('.card').count()).toBeGreaterThan(0);
});

test('Home is the default landing tab', async ({ page }) => {
  await page.goto(url);
  await expect(page.locator('#tab-home')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#home-view')).toBeVisible();
});

test('score rings render on cards', async ({ page }) => {
  await gotoEvidence(page);
  await expect(page.locator('.card .score-box').first()).toBeVisible();
  // Rank total ring + 3 sub-rings = 4 ring items per card.
  expect(await page.locator('.card').first().locator('.ring-item').count()).toBe(4);
});

test('search filters the list to matching records', async ({ page }) => {
  await gotoEvidence(page);
  const before = await page.locator('.card').count();
  await page.fill('#q', 'retatrutide');
  await page.waitForTimeout(400); // input is debounced ~150ms
  const after = await page.locator('.card').count();
  expect(after).toBeGreaterThan(0);
  expect(after).toBeLessThanOrEqual(before);
  for (const t of await page.locator('.card').allInnerTexts()) {
    expect(t.toLowerCase()).toContain('retatrutide');
  }
});

test('clicking a card opens the detail dialog; Escape closes it', async ({ page }) => {
  await gotoEvidence(page);
  await page.locator('.card').first().click();
  const dialog = page.locator('#modal[role="dialog"]');
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});

test('a card is keyboard-operable (focus + Enter opens the dialog)', async ({ page }) => {
  await gotoEvidence(page);
  await page.locator('.card').first().focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#modal[role="dialog"]')).toBeVisible();
});

test('modal traps Tab focus inside the dialog', async ({ page }) => {
  await gotoEvidence(page);
  await page.locator('.card').first().click();
  await expect(page.locator('#modal[role="dialog"]')).toBeVisible();
  // Tab several times; focus must stay within the dialog, never escape to the page.
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press('Tab');
    const inside = await page.evaluate(() =>
      document.getElementById('modal').contains(document.activeElement));
    expect(inside).toBe(true);
  }
});

test('switching to Bioactives renders the molecule grid', async ({ page }) => {
  await page.goto(url);
  await page.click('#tab-molecules');
  await expect(page.locator('.mol-card').first()).toBeVisible();
});

test('active tab exposes aria-selected for screen readers', async ({ page }) => {
  await page.goto(url);
  await page.click('#tab-molecules');
  await expect(page.locator('#tab-molecules')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#tab-evidence')).toHaveAttribute('aria-selected', 'false');
});
