import { expect, test } from "@playwright/test"

const baseURL = process.env.BASE_URL

test("Phase 5 related docs and quick-open palette", async ({ page }) => {
  expect(baseURL, "BASE_URL must point at a built Quartz fixture").toBeTruthy()

  await page.goto(`${baseURL}/`)

  const related = page.locator(".brain-related-docs")
  await expect(related).toBeVisible()
  await expect(page.locator(".brain-related-docs-item")).toContainText("Demo Vault Doc")
  await page.screenshot({ path: ".playwright-mcp/phase5-related-docs.png", fullPage: true })

  await page.keyboard.press("Control+P")
  const dialog = page.locator("#brain-cmdk-root")
  await expect(dialog).toHaveJSProperty("open", true)
  await expect(page.locator(".brain-cmdk-chips")).toBeVisible()
  await expect(page.locator('.brain-cmdk-chip[data-brain-source="vault"]')).toHaveAttribute(
    "aria-pressed",
    "true",
  )

  await page.locator(".brain-cmdk-input").fill("Demo")
  await expect(page.locator(".brain-cmdk-result").first()).toContainText("Demo Vault Doc")
  await page.screenshot({ path: ".playwright-mcp/phase5-command-palette.png", fullPage: true })

  await page.keyboard.press("Escape")
  await expect(dialog).toHaveJSProperty("open", false)

  await page.keyboard.press("Control+K")
  await expect(page.locator(".search-container.active")).toBeVisible()
  await expect(dialog).toHaveJSProperty("open", false)
})
