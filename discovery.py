"""
Challenge Discovery Module
Reads and parses challenge information from the Gray Swan Arena page.
"""

import asyncio
import re
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from browser import BrowserController


@dataclass
class Scenario:
    """Represents a single scenario within a challenge."""
    name: str
    type: str  # DIRECT, IPI, etc.
    attack_type: str  # Direct attack, etc.
    breaks: str  # e.g., "0/1"
    solved: bool = False
    description: str = ""
    success_criteria: str = ""


@dataclass
class Challenge:
    """Represents a challenge in the arena."""
    name: str
    description: str = ""
    scenarios: List[Scenario] = field(default_factory=list)
    model: str = ""
    total_scenarios: int = 0
    solved_scenarios: int = 0
    url: str = ""
    completion_indicator: str = ""  # e.g., "SUBMIT BREAK", "0/5 BREAKS"
    raw_content: str = ""


class ChallengeDiscovery:
    """Discovers and parses challenges from the arena page."""

    def __init__(self, browser: BrowserController):
        self.browser = browser
        self.challenges: List[Challenge] = []
        self.current_index: int = 0

    async def discover_challenges(self) -> List[Challenge]:
        """Discover all available challenges from the Hazard Hunt page."""
        print("[Discovery] Discovering challenges from Hazard Hunt Wave 2...")

        hazard_hunt_url = "https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26"
        
        # Check if we're already on the hazard hunt page
        current_url = self.browser.page.url
        if "hazard-hunt" not in current_url:
            print(f"[Discovery] Navigating to: {hazard_hunt_url}")
            await self.browser.page.goto(hazard_hunt_url, wait_until="domcontentloaded", timeout=60000)
            await self.browser.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
        else:
            print("[Discovery] Already on hazard hunt page, skipping navigation")
            # Refresh to ensure we have latest data
            await self.browser.page.reload(wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

        # Scroll to load all content
        await self._scroll_to_load_all()
        
        # Extract all unique scenarios (the actual challenges)
        content = await self.browser.read_page_content()
        scenarios = self._parse_scenarios_as_challenges(content)
        
        print(f"[Discovery] Found {len(scenarios)} unique scenarios (challenges)")

        # Each scenario is a challenge with 24 breaks
        # No need to navigate into each - all info is in the list view
        self.challenges = []
        for scenario_data in scenarios:
            name = scenario_data['name']
            challenge = Challenge(
                name=name,
                description=scenario_data.get('description', ''),
                scenarios=[],  # Each challenge has 24 breaks, not sub-scenarios
                model=scenario_data.get('model', 'Unknown'),
                total_scenarios=scenario_data.get('breaks', 24),  # 24 breaks per challenge
                solved_scenarios=0,
                completion_indicator=f"0/{scenario_data.get('breaks', 24)} BREAKS",
                raw_content=content
            )
            self.challenges.append(challenge)

        print(f"[Discovery] Parsed {len(self.challenges)} challenges")
        for i, c in enumerate(self.challenges):
            print(f"  [{i}] {c.name} - {c.total_scenarios} breaks - Model: {c.model}")

        return self.challenges

    async def _scroll_to_load_all(self):
        """Scroll the page to load all challenges."""
        print("[Discovery] Scrolling to load all challenges...")
        last_height = 0
        for i in range(80):  # Max 80 scrolls for 48*24=1152 breaks
            # Scroll down
            await self.browser.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)
            
            # Check if we've reached the bottom
            new_height = await self.browser.page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                print(f"[Discovery] Reached bottom after {i+1} scrolls")
                break
            last_height = new_height
        
        # Scroll back up
        await self.browser.page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)

    def _parse_scenarios_as_challenges(self, content: str) -> List[Dict]:
        """Parse scenarios from the list view as individual challenges."""
        scenarios = []
        lines = content.split('\n')
        
        # Also extract model from content
        model = self._extract_model(content)
        
        # Look for scenario patterns in the list view
        # Each scenario appears as: Name, DIRECT, Attack type, 0/23, 0/23 breaks
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Skip filter tabs and UI elements
            skip_terms = ['Wave 1', 'Wave 2', 'LIVE', 'NOV 16-23', 'CURRENT RANK', 'ALL CHALLENGES',
                        'HAZARD HUNT', 'SCENARIOS', 'TIME LEFT', 'PRIZE POOL',
                        'YOUR NEXT CHALLENGE', 'CONTINUE BREAKING', 'BREAKING',
                        'DASHBOARD', 'CHALLENGES', 'TRAINING', 'LEADERBOARD',
                        'WELCOME BACK', 'FROM THE COMMUNITY', 'ONLINE',
                        'NEWS', 'ARENA-UPDATES', 'CHANGELOG', 'YOUR STREAK',
                        'SEE FULL HISTORY', 'BREAKS', 'POINTS', 'RANK',
                        'CERTIFICATES', 'VIEW ALL', 'LUCKY BREAK', 'PAYDAY',
                        'PARTICIPANT', 'CONTINUE', 'JOIN THE DISCORD',
                        'SERVER', 'TOGGLE SIDEBAR', 'ATTACKING',
                        'SUBMISSIONS', 'SUBMIT BREAK', 'HARMFUL TASK',
                        'SEE SUCCESS CRITERIA']
            
            if line in skip_terms:
                i += 1
                continue
            
            # Skip lines that are just numbers or dates
            if line.isdigit() or re.match(r'^\d+[hdw]$', line) or re.match(r'^[A-Z]{3}$', line):
                i += 1
                continue
            
            # Look for scenario name followed by DIRECT and break info
            if line and len(line) > 5:
                # Check if next lines match scenario pattern
                if i + 3 < len(lines):
                    type_line = lines[i + 1].strip()
                    attack_line = lines[i + 2].strip()
                    breaks_line = lines[i + 3].strip()
                    
                    # Check if this looks like a scenario entry
                    if (type_line == 'DIRECT' and
                        ('attack' in attack_line.lower() or True) and  # attack type line
                        re.search(r'\d+/\d+', breaks_line)):
                        
                            # Extract breaks count
                            match = re.search(r'(\d+)/(\d+)', breaks_line)
                            total_breaks = int(match.group(2)) if match else 24
                            
                            scenario = {
                                'name': line,
                                'type': type_line,
                                'attack_type': attack_line,
                                'breaks': total_breaks,
                                'description': attack_line,
                                'model': model
                            }
                            
                            # Avoid duplicates
                            if not any(s['name'] == line for s in scenarios):
                                scenarios.append(scenario)
                                print(f"  Found scenario: {line} ({total_breaks} breaks)")
                            
                            i += 4
                            continue
            
            i += 1
        
        return scenarios

    def _extract_model(self, content: str) -> str:
        """Extract the assigned model name."""
        # Look for "Attacking: ModelName" pattern
        match = re.search(r'Attacking:\s*([^\n]+)', content)
        if match:
            return match.group(1).strip()

        # Fallback: look for model names in content
        model_patterns = [
            r'(Silver Tardigrade Plus Ultra)',
            r'(Brass Ant Iridescent)',
            r'(Fire Hippo Mega)',
            r'(Diamond Fish Hyper)',
            r'(Aluminum Ant Galactic)',
            r'(Aluminum Crow Magical)',
            r'(Air Antelope Ethereal)',
            r'(GPT-\d+)',
            r'(Claude \d+)',
            r'(Llama \d+)',
        ]
        for pattern in model_patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1)

        return "Unknown"

    def _extract_completion_indicator(self, content: str) -> str:
        """Extract the official completion indicator."""
        indicators = [
            "SUBMIT BREAK",
            "SUBMIT",
            "COMPLETE",
            "FINISH",
            "SOLVED",
        ]

        for indicator in indicators:
            if indicator in content:
                return indicator

        # Look for X/Y BREAKS pattern
        match = re.search(r'(\d+)/(\d+)\s*BREAKS', content)
        if match:
            return f"{match.group(1)}/{match.group(2)} BREAKS"

        return ""

    def _extract_description(self, content: str) -> str:
        """Extract challenge description."""
        # Look for "HARMFUL TASK" section
        match = re.search(r'HARMFUL TASK\s*\n([^\n]+(?:\n[^\n]+)*)', content)
        if match:
            return match.group(1).strip()

        # Fallback: first substantial paragraph
        lines = [l.strip() for l in content.split('\n') if l.strip() and len(l.strip()) > 20]
        return lines[0] if lines else ""

    def get_next_challenge(self) -> Optional[Challenge]:
        """Get the next undiscovered/unsolved challenge."""
        for i in range(self.current_index, len(self.challenges)):
            challenge = self.challenges[i]
            if challenge.solved_scenarios < challenge.total_scenarios:
                self.current_index = i
                return challenge
        return None

    def get_challenge_by_name(self, name: str) -> Optional[Challenge]:
        """Get a specific challenge by name."""
        for c in self.challenges:
            if c.name.lower() == name.lower():
                return c
        return None

    def mark_scenario_solved(self, challenge_name: str, scenario_name: str):
        """Mark a scenario as solved."""
        challenge = self.get_challenge_by_name(challenge_name)
        if challenge:
            for s in challenge.scenarios:
                if s.name.lower() == scenario_name.lower():
                    s.solved = True
                    challenge.solved_scenarios = sum(1 for sc in challenge.scenarios if sc.solved)
                    break

    def print_summary(self):
        """Print a summary of all discovered challenges."""
        print("\n" + "=" * 60)
        print("CHALLENGE DISCOVERY SUMMARY")
        print("=" * 60)
        total_breaks = sum(c.total_scenarios for c in self.challenges)
        solved_breaks = sum(c.solved_scenarios for c in self.challenges)
        print(f"Total: {len(self.challenges)} challenges, {total_breaks} breaks ({solved_breaks} solved)")
        print("=" * 60)
        for c in self.challenges:
            status = "✓ COMPLETE" if c.solved_scenarios >= c.total_scenarios and c.total_scenarios > 0 else "IN PROGRESS"
            print(f"\n{c.name} [{status}]")
            print(f"  Model: {c.model}")
            print(f"  Breaks: {c.solved_scenarios}/{c.total_scenarios}")
            print(f"  Completion: {c.completion_indicator}")
        print("=" * 60)


async def main():
    """Test the challenge discovery."""
    print("=" * 50)
    print("Challenge Discovery Test")
    print("=" * 50)

    async with BrowserController(headless=False) as browser:
        await browser.open_arena()
        logged_in = await browser.wait_for_login(timeout=300000)

        if not logged_in:
            print("[Test] Login failed")
            return

        # Navigate to arena page
        await browser.get_challenge_page()

        # Discover challenges
        discovery = ChallengeDiscovery(browser)
        challenges = await discovery.discover_challenges()

        # Print summary
        discovery.print_summary()

        # Test getting next challenge
        next_challenge = discovery.get_next_challenge()
        if next_challenge:
            print(f"\n[Test] Next challenge to work on: {next_challenge.name}")

        print("\n[Test] Keeping browser open for 30 seconds...")
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())