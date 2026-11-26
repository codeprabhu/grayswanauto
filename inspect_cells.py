import asyncio
import json
from playwright.async_api import async_playwright

URL = "https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26"

async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            ".browser-profile",
            headless=False
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded")

        await page.locator("table").first.wait_for(timeout=20000)

        result = await page.locator("table").first.evaluate("""table => {
          const describe = el => ({
            tag: el.tagName,
            text: (el.innerText || '').trim(),
            html: el.outerHTML.slice(0, 2000),
            title: el.getAttribute('title'),
            aria: el.getAttribute('aria-label'),
            class: el.className
          });

          return {
            headers: [...table.querySelectorAll('thead th')].map(describe),
            firstRows: [...table.querySelectorAll('tbody tr')]
              .slice(0, 2)
              .map(row => [...row.children].map(describe)),
            lastRows: [...table.querySelectorAll('tbody tr')]
              .slice(-2)
              .map(row => [...row.children].map(describe))
          };
        }""")

        with open("data/breaks_cells_inspection.json", "w") as f:
            json.dump(result, f, indent=2)

        print("Saved data/breaks_cells_inspection.json")
        print(json.dumps(result, indent=2)[:10000])
        await context.close()

asyncio.run(main())
