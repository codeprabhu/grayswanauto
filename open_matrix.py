import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26"

async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            ".browser-profile",
            headless=False
        )

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await page.goto(URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)

            print("Page:", page.url)
            print("Title:", await page.title())

            # Find the Breaks Overview control by its visible text.
            control = page.locator("button").filter(has_text="0/943 BREAKS")


            print("Breaks control matches:", await control.count())

            if await control.count() == 0:
                print("Could not find the Breaks Overview control.")
                print("Visible buttons:")
                print(await page.locator("button").all_inner_texts())
                await page.screenshot(path="data/navigation_debug.png")
                print("Saved data/navigation_debug.png")
                return

            await control.first.click()
            print("Clicked Breaks Overview.")

            # Wait for the matrix to render.
            await page.wait_for_timeout(3000)

            table = page.locator("table").first
            print("Table count:", await page.locator("table").count())

            if await table.count() == 0:
                print("Matrix table did not appear.")
                await page.screenshot(path="data/navigation_debug.png")
                print("Saved data/navigation_debug.png")
                return

            await table.wait_for(state="visible", timeout=15000)

            matrix = await table.evaluate("""table => {
                const headers = [...table.querySelectorAll('thead th')]
                    .map(el => el.innerText.trim());

                const rows = [...table.querySelectorAll('tbody tr')];

                return {
                    models: headers.slice(1, 24),
                    scenarios: rows.slice(0, 41).map(row => {
                        const cells = [...row.children];
                        return {
                            name: cells[0].innerText.trim(),
                            cells: cells.slice(1, 24).map(
                                cell => cell.innerText.trim()
                            ),
                            total: cells[24]?.innerText.trim() || ""
                        };
                    })
                };
            }""")

            output = {
                "challenge": "Hazard Hunt November '26",
                "scenario_count": len(matrix["scenarios"]),
                "model_count": len(matrix["models"]),
                "matrix": matrix
            }

            Path("data").mkdir(exist_ok=True)
            Path("data/hazard_hunt_inventory.json").write_text(
                json.dumps(output, indent=2)
            )

            print("SUCCESS")
            print("Scenarios:", output["scenario_count"])
            print("Models:", output["model_count"])
            print("Combinations:",
                  output["scenario_count"] * output["model_count"])
            print("Saved: data/hazard_hunt_inventory.json")

        finally:
            await context.close()

asyncio.run(main())
