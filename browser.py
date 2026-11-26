"""
Browser Controller for Gray Swan Arena
Uses Playwright with Firefox persistent profile to automate browser interactions.
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


class BrowserController:
    def __init__(self, profile_dir: str = "./browser_profile", headless: bool = False):
        self.base_profile_dir = Path(profile_dir).resolve()
        self.headless = headless
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        self.base_url = "https://app.grayswan.ai"

    def _get_unique_profile_dir(self):
        """Get a unique profile directory to avoid lock issues."""
        timestamp = int(time.time() * 1000)
        return self.base_profile_dir.parent / f"{self.base_profile_dir.name}_{timestamp}"

    async def launch(self):
        """Launch browser with persistent profile."""
        # Use unique profile dir each time to avoid lock issues
        profile_dir = self._get_unique_profile_dir()
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy the base profile to the new directory to preserve login state
        base_profile = self.base_profile_dir
        if base_profile.exists() and base_profile != profile_dir:
            # Copy the entire profile to preserve login state
            for item in base_profile.iterdir():
                if item.name not in ["SingletonLock", "SingletonCookie", ".parentlock", "lock"]:
                    src = item
                    dst = profile_dir / item.name
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
        
        # Clean up any existing lock files in the new profile dir
        for lock_file in ["SingletonLock", "SingletonCookie", ".parentlock", "lock"]:
            lock_path = profile_dir / lock_file
            if lock_path.exists():
                try:
                    lock_path.unlink()
                    print(f"[Browser] Removed stale lock file: {lock_file}")
                except Exception as e:
                    print(f"[Browser] Warning: Could not remove {lock_file}: {e}")

        print(f"[Browser] Launching browser with profile: {profile_dir}")

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            args=[
                "--start-maximized", 
                "--no-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1920, "height": 1080},
            ignore_default_args=["--enable-automation"],
        )
        self.context = self.browser
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        # Set longer timeouts for navigation
        self.page.set_default_navigation_timeout(60000)
        self.page.set_default_timeout(30000)

        print("[Browser] Browser launched successfully")
        return self

    async def open_arena(self):
        """Navigate to Gray Swan Arena."""
        print(f"[Browser] Navigating to {self.base_url}")
        try:
            await self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=180000)
            print("[Browser] Page loaded")
        except Exception as e:
            print(f"[Browser] Navigation warning: {e}")

        # Wait for page to fully render
        await self.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(5)

        return self

    async def wait_for_login(self, timeout: int = 300000):
        """
        Wait for user to manually log in.
        Checks for indicators that login is complete.
        First checks if already logged in (persistent profile).
        """
        print("[Browser] Checking login status...")

        # Navigate to login page if we're on the main landing page
        try:
            # Check if we're on the main landing page (not logged in)
            on_landing = await self.page.evaluate("""() => {
                const hasText = (selector, text) => {
                    const el = document.querySelector(selector);
                    return el && el.textContent && el.textContent.includes(text);
                };
                
                // Main landing page has these but NOT login buttons
                const hasLoginButtons = hasText('button', 'Continue with Google') ||
                                       hasText('button', 'Continue with Discord') ||
                                       hasText('button', 'Continue with email');
                const isLanding = window.location.pathname === '/arena' ||
                                 window.location.pathname === '/' ||
                                 (hasText('a', 'LOGIN') && !hasLoginButtons);
                return isLanding;
            }""")
            
            if on_landing:
                print("[Browser] On landing page - navigating to login...")
                # Click the LOGIN link in the navbar
                try:
                    login_link = await self.page.wait_for_selector('a:has-text("LOGIN")', timeout=5000)
                    if login_link:
                        await login_link.click()
                        await self.page.wait_for_load_state("domcontentloaded")
                        await asyncio.sleep(2)
                        print("[Browser] Navigated to login page")
                except Exception:
                    # Try direct navigation
                    await self.page.goto("https://app.grayswan.ai/login", wait_until="domcontentloaded", timeout=180000)
                    await asyncio.sleep(2)
        except Exception as e:
            print(f"[Browser] Navigation check error: {e}")

        # Now check if we're on the actual login page
        try:
            on_login_page = await self.page.evaluate("""() => {
                const hasText = (selector, text) => {
                    const el = document.querySelector(selector);
                    return el && el.textContent && el.textContent.includes(text);
                };
                
                // Actual login page has these buttons
                return hasText('button', 'Continue with Google') ||
                       hasText('button', 'Continue with Discord') ||
                       hasText('button', 'Continue with email') ||
                       hasText('a', 'Sign Up') ||
                       window.location.pathname === '/login' ||
                       window.location.pathname === '/signin';
            }""")
            
            if on_login_page:
                print("[Browser] On login page - waiting for manual login...")
            else:
                # Check if already logged in (post-login indicators - more specific)
                already_logged_in = await self.page.evaluate("""() => {
                    const hasText = (selector, text) => {
                        const el = document.querySelector(selector);
                        return el && el.textContent && el.textContent.includes(text);
                    };
                    
                    // More specific post-login indicators
                    return document.querySelector('[data-testid="user-menu"]') !== null ||
                           document.querySelector('.user-profile') !== null ||
                           (hasText('button', 'Challenge') && !hasText('a', 'LOGIN')) ||
                           document.querySelector('[href*="/dashboard"]') !== null ||
                           localStorage.getItem('auth_token') !== null ||
                           sessionStorage.getItem('auth_token') !== null ||
                           window.location.pathname.includes('/challenge');
                }""")
                
                if already_logged_in:
                    print("[Browser] Already logged in (persistent profile)!")
                    return True
        except Exception as e:
            print(f"[Browser] Check error: {e}")

        print("[Browser] Not logged in. Waiting for manual login...")
        print("[Browser] Please log in to Gray Swan Arena in the browser window.")
        print(f"[Browser] Timeout: {timeout/1000} seconds")

        try:
            # Wait for common post-login indicators
            await self.page.wait_for_function(
                """() => {
                    const hasText = (selector, text) => {
                        const el = document.querySelector(selector);
                        return el && el.textContent && el.textContent.includes(text);
                    };
                    
                    // Check for user menu, profile, or challenge page elements
                    // Must NOT be on login/signup page
                    const isLoginPage = hasText('button', 'Continue with Google') ||
                                        hasText('button', 'Continue with Discord') ||
                                        hasText('button', 'Continue with email') ||
                                        hasText('a', 'Sign Up') ||
                                        window.location.pathname === '/' ||
                                        window.location.pathname === '/login' ||
                                        window.location.pathname === '/signin';
                    
                    if (isLoginPage) return false;
                    
                    // Check for post-login indicators
                    return document.querySelector('[data-testid="user-menu"]') !== null ||
                           document.querySelector('.user-profile') !== null ||
                           document.querySelector('a[href*="/challenge"]') !== null ||
                           hasText('button', 'Challenge') ||
                           document.querySelector('[href*="/arena"]') !== null ||
                           document.querySelector('[href*="/dashboard"]') !== null ||
                           localStorage.getItem('auth_token') !== null ||
                           sessionStorage.getItem('auth_token') !== null ||
                           window.location.pathname.includes('/challenge') ||
                           window.location.pathname.includes('/arena');
                }""",
                timeout=timeout
            )
            print("[Browser] Login detected!")
            return True
        except Exception as e:
            print(f"[Browser] Login timeout or error: {e}")
            return False

    async def read_page_content(self) -> str:
        """Read the rendered page content (text)."""
        if not self.page:
            raise RuntimeError("Page not initialized. Call launch() first.")

        content = await self.page.content()
        # Also get visible text for easier parsing
        text_content = await self.page.evaluate("() => document.body.innerText")
        return text_content

    async def read_page_html(self) -> str:
        """Read the full HTML of the page."""
        if not self.page:
            raise RuntimeError("Page not initialized. Call launch() first.")
        return await self.page.content()

    async def send_message(self, message: str, selector: str = None):
        """
        Send a message through the chat interface.
        Tries multiple common selectors for chat input.
        """
        if not self.page:
            raise RuntimeError("Page not initialized. Call launch() first.")

        # Common chat input selectors - adjust based on actual Gray Swan Arena UI
        selectors = [
            selector,
            'textarea[placeholder*="message" i]',
            'textarea[placeholder*="chat" i]',
            'input[placeholder*="message" i]',
            'input[placeholder*="chat" i]',
            'div[contenteditable="true"]',
            'textarea',
            '#chat-input',
            '.chat-input',
            '[data-testid="chat-input"]',
        ]

        input_element = None
        for sel in selectors:
            if not sel:
                continue
            try:
                input_element = await self.page.wait_for_selector(sel, timeout=2000)
                if input_element:
                    print(f"[Browser] Found chat input with selector: {sel}")
                    break
            except Exception:
                continue

        if not input_element:
            raise RuntimeError("Could not find chat input element. Please specify selector.")

        # Clear and type message - use force click and fill
        try:
            await input_element.click(force=True, timeout=5000)
        except Exception:
            # Try scrolling into view first
            await input_element.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            await input_element.click(force=True, timeout=5000)
        
        await input_element.fill("")
        await input_element.type(message, delay=50, timeout=120000)

        # Find and click send button
        send_selectors = [
            'button:has-text("Send")',
            'button[type="submit"]',
            'button[aria-label*="send" i]',
            '.send-button',
            '[data-testid="send-button"]',
            'button:has(svg)',
        ]

        sent = False
        for sel in send_selectors:
            try:
                send_btn = await self.page.wait_for_selector(sel, timeout=1000)
                if send_btn:
                    await send_btn.click(force=True)
                    print("[Browser] Message sent")
                    sent = True
                    break
            except Exception:
                continue
        
        if not sent:
            # Try pressing Enter
            try:
                await input_element.press("Enter")
                print("[Browser] Message sent via Enter key")
            except Exception:
                # Last resort: JavaScript submit
                await self.page.evaluate("""() => {
                    const textarea = document.querySelector('textarea');
                    if (textarea) {
                        const form = textarea.closest('form');
                        if (form) form.submit();
                    }
                }""")
                print("[Browser] Message sent via JS submit")

        # Wait for response to start generating
        await asyncio.sleep(2)

    async def capture_response(self, timeout: int = 120000) -> str:
        """
        Capture the model's response from the chat.
        Waits for response to complete (stops streaming).
        """
        if not self.page:
            raise RuntimeError("Page not initialized. Call launch() first.")

        print("[Browser] Waiting for model response...")

        # Wait for response to appear and finish streaming
        # This looks for common patterns: streaming indicator disappears, or new message appears
        try:
            await self.page.wait_for_function(
                """() => {
                    // Check if streaming has stopped (no spinning indicators)
                    const streaming = document.querySelector('[data-testid="streaming"]') ||
                                     document.querySelector('.streaming') ||
                                     document.querySelector('[aria-busy="true"]') ||
                                     document.querySelector('.generating');
                    return streaming === null;
                }""",
                timeout=timeout
            )
        except Exception:
            print("[Browser] Streaming indicator not found or timed out, proceeding...")

        # Get the latest message from the chat
        # Try multiple selectors for message containers
        message_selectors = [
            '[data-testid="message"]:last-child',
            '.message:last-child',
            '.chat-message:last-child',
            '[role="log"] > div:last-child',
            'main',  # Fallback
        ]

        for sel in message_selectors:
            try:
                elements = await self.page.query_selector_all(sel)
                if elements:
                    last_msg = elements[-1]
                    text = await last_msg.inner_text()
                    if text and len(text) > 10:  # Non-trivial response
                        print(f"[Browser] Captured response ({len(text)} chars)")
                        return text.strip()
            except Exception:
                continue

        # Fallback: get all visible text and extract last substantial chunk
        full_text = await self.page.evaluate("() => document.body.innerText")
        return full_text[-5000:] if len(full_text) > 5000 else full_text

    async def get_challenge_page(self) -> str:
        """Navigate to and read the challenge page."""
        print("[Browser] Navigating to challenge page...")
        # Try multiple possible challenge URLs
        challenge_urls = [
            f"{self.base_url}/challenge",
            f"{self.base_url}/arena",
            f"{self.base_url}/dashboard",
            f"{self.base_url}/challenges",
        ]
        
        for url in challenge_urls:
            try:
                print(f"[Browser] Trying: {url}")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=180000)
                await self.page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(2)
                content = await self.read_page_content()
                if content and len(content) > 100:
                    print(f"[Browser] Successfully loaded: {url}")
                    return content
            except Exception as e:
                print(f"[Browser] Failed to load {url}: {e}")
                continue
        
        # If all fail, return current page content
        print("[Browser] Using current page content")
        return await self.read_page_content()

    async def enter_challenge(self, challenge_name: str = "THE LABYRINTH") -> bool:
        """Click on a specific challenge to enter it."""
        print(f"[Browser] Looking for challenge: {challenge_name}")
        
        # Strategy 1: Use data-behavior-id attribute (unique per challenge)
        # Convert challenge name to behavior ID format
        behavior_id = challenge_name.lower().replace(" ", "-").replace(":", "").replace("'", "")
        behavior_id = behavior_id.replace("--", "-")
        
        # Try exact behavior ID
        selectors = [
            f'[data-behavior-id="{behavior_id}"]',
            f'button[data-behavior-id*="{behavior_id[:30]}"]',
            f'[data-behavior-switch][data-behavior-id*="{behavior_id[:30]}"]',
        ]
        
        for sel in selectors:
            try:
                element = await self.page.wait_for_selector(sel, timeout=3000)
                if element:
                    await element.click()
                    print(f"[Browser] Clicked challenge (behavior-id): {challenge_name}")
                    await self.page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(3)
                    return True
            except Exception:
                continue
        
        # Strategy 2: Exact text match
        selectors = [
            f'a:has-text("{challenge_name}")',
            f'button:has-text("{challenge_name}")',
            f'text="{challenge_name}"',
        ]
        
        for sel in selectors:
            try:
                element = await self.page.wait_for_selector(sel, timeout=3000)
                if element:
                    await element.click()
                    print(f"[Browser] Clicked challenge (exact): {challenge_name}")
                    await self.page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(3)
                    return True
            except Exception:
                continue
        
        # Strategy 3: Find all challenge buttons and match by text content
        try:
            # Get all challenge buttons
            buttons = await self.page.query_selector_all('button[data-behavior-switch][data-behavior-id]')
            for btn in buttons:
                text = await btn.inner_text()
                behavior_id_attr = await btn.get_attribute("data-behavior-id")
                
                # Check if this button matches our challenge
                if (challenge_name.lower() in text.lower() or 
                    text.lower() in challenge_name.lower() or
                    self._text_similarity(text, challenge_name) > 0.5):
                    
                    # Scroll into view and click
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5)
                    await btn.click(force=True)
                    print(f"[Browser] Clicked challenge (button match): {text[:50]} (id: {behavior_id_attr})")
                    await self.page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(3)
                    return True
        except Exception as e:
            print(f"[Browser] Button match failed: {e}")
        
        print(f"[Browser] Could not find challenge: {challenge_name}")
        return False

    def _text_similarity(self, a: str, b: str) -> float:
        """Simple text similarity check."""
        a_words = set(a.lower().split())
        b_words = set(b.lower().split())
        if not a_words or not b_words:
            return 0.0
        intersection = a_words & b_words
        union = a_words | b_words
        return len(intersection) / len(union)

    async def list_challenges(self) -> list:
        """List available challenges on the page."""
        print("[Browser] Scanning for challenges...")
        challenges = []
        
        # Look for challenge cards/links
        try:
            elements = await self.page.query_selector_all('a[href*="challenge"], a[href*="arena"], button:has-text("CONTINUE"), text=/CHALLENGE/i')
            for el in elements:
                text = await el.inner_text()
                href = await el.get_attribute("href")
                if text and len(text) > 3:
                    challenges.append({"text": text.strip(), "href": href})
        except Exception as e:
            print(f"[Browser] Error scanning challenges: {e}")
        
        return challenges

    async def screenshot(self, path: str = "screenshot.png"):
        """Take a screenshot for debugging."""
        if self.page:
            await self.page.screenshot(path=path, full_page=True)
            print(f"[Browser] Screenshot saved to {path}")

    async def close(self):
        """Close browser and cleanup."""
        print("[Browser] Closing browser...")
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        print("[Browser] Closed")

    async def __aenter__(self):
        await self.launch()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


async def main():
    """Test the browser controller."""
    print("=" * 50)
    print("Gray Swan Arena Browser Controller Test")
    print("=" * 50)

    async with BrowserController(headless=False) as browser:
        # Open the arena
        await browser.open_arena()

        # Wait for manual login
        logged_in = await browser.wait_for_login(timeout=300000)  # 5 minutes

        if not logged_in:
            print("[Test] Login not detected. Taking screenshot for debugging.")
            await browser.screenshot("login_debug.png")
            print("[Test] Check the browser window and screenshot.")
            return

        print("\n[Test] Login successful! Reading challenge page...")

        # Read challenge page
        challenge_content = await browser.get_challenge_page()
        print(f"\n[Test] Challenge page content ({len(challenge_content)} chars):")
        print("-" * 50)
        print(challenge_content[:2000])
        print("-" * 50)
        print("... (truncated)")

        # Test sending a message (optional)
        print("\n[Test] Testing chat interface...")
        try:
            await browser.send_message("Hello, this is a test message.")
            response = await browser.capture_response(timeout=30000)
            print(f"[Test] Response: {response[:500]}")
        except Exception as e:
            print(f"[Test] Chat test failed (expected if not on challenge page): {e}")

        print("\n[Test] Keeping browser open for 30 seconds for manual verification...")
        await asyncio.sleep(30)

    print("\n[Test] Done!")


if __name__ == "__main__":
    asyncio.run(main())