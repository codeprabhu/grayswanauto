import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26"

async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=".browser-profile",
            headless=False,
            viewport=None
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)

        print("\nBrowser opened.")
        print("If needed, navigate to the November Hazard Hunt page.")
        print("Open the BREAKS OVERVIEW shown in your screenshot.")
        input("Once the overview is visible, press ENTER here...")

        result = await page.evaluate("""() => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };

          const elements = [...document.querySelectorAll(
            '[role="dialog"], [role="table"], [role="grid"], [role="row"], ' +
            '[role="columnheader"], [role="gridcell"], table, tr, th, td'
          )].filter(visible);

          return {
            url: location.href,
            title: document.title,
            bodyText: document.body.innerText,
            elements: elements.map(el => ({
              tag: el.tagName,
              role: el.getAttribute('role'),
              text: (el.innerText || '').trim().slice(0, 500),
              ariaLabel: el.getAttribute('aria-label'),
              title: el.getAttribute('title'),
              className: typeof el.className === 'string'
                ? el.className.slice(0, 300) : '',
              attributes: [...el.attributes]
                .filter(a => a.name.startsWith('data-'))
                .map(a => [a.name, a.value])
            }))
          };
        }""")

        out = Path("data/breaks_overview_dom.json")
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False))

        print(f"\nSaved DOM inspection to {out}")
        print("Page title:", result["title"])
        print("Captured elements:", len(result["elements"]))
        print("Visible text preview:")
        print(result["bodyText"][:3500])

        await context.close()

asyncio.run(main())
