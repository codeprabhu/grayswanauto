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

        requests = []

        page.on("response", lambda response: requests.append({
            "url": response.url,
            "status": response.status,
            "content_type": response.headers.get("content-type", "")
        }) if any(x in response.url.lower() for x in [
            "api", "graphql", "challenge", "scenario", "break"
        ]) else None)

        await page.goto(URL, wait_until="domcontentloaded")
        await page.locator("table").first.wait_for(timeout=60000)
        await page.wait_for_timeout(3000)

        result = {
            "url": page.url,
            "title": await page.title(),
            "matching_responses": requests
        }

        with open("data/page_data_sources.json", "w") as f:
            json.dump(result, f, indent=2)

        print(json.dumps(result, indent=2))
        await context.close()

asyncio.run(main())
