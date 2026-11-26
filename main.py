"""
Main Scheduler - Orchestrates the complete automator pipeline.
Runs a single challenge with continuous feedback loop until completion.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from browser import BrowserController
from discovery import ChallengeDiscovery, Challenge
from llm import LLMClient, get_llm_client, close_llm_client, create_ollama_client
from agent import Agent, ChallengeResult, AgentStatus


STATE_FILE = Path("./automator_state.json")


class Automator:
    """Main scheduler that runs a single challenge with continuous feedback loop."""

    def __init__(
        self,
        headless: bool = False,
        max_iterations_per_challenge: int = 20,
        api_key: str = None,
        challenge_filter: str = None,  # e.g., "Extract" to only run Extract challenges
    ):
        self.headless = headless
        self.max_iterations = max_iterations_per_challenge
        self.api_key = api_key or os.environ.get("NVIDIA_API_KEY")
        self.challenge_filter = challenge_filter
        
        self.browser: Optional[BrowserController] = None
        self.llm: Optional[LLMClient] = None
        self.discovery: Optional[ChallengeDiscovery] = None
        
        # Persistent state
        self.state: Dict[str, Any] = self.load_state()
        self.all_challenges: List[Challenge] = []
        self.challenge_index: Dict[str, Challenge] = {}

    def load_state(self) -> Dict[str, Any]:
        """Load persistent state from disk."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[State] Warning: Could not load state: {e}")
        return {
            "completed_challenges": {},
            "current_challenge": None,
            "total_completed": 0,
            "total_failed": 0,
            "total_iterations": 0,
        }

    def save_state(self):
        """Save persistent state to disk."""
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"[State] Warning: Could not save state: {e}")

    def is_completed(self, challenge_name: str) -> bool:
        """Check if a challenge is marked as completed."""
        return challenge_name in self.state["completed_challenges"]

    def mark_completed(self, challenge_name: str, result: ChallengeResult):
        """Mark a challenge as completed."""
        self.state["completed_challenges"][challenge_name] = {
            "completed_at": datetime.now().isoformat(),
            "iterations": result.iterations_used,
            "success": result.success_criteria_met,
            "status": result.status.value,
        }
        if result.success_criteria_met:
            self.state["total_completed"] += 1
        else:
            self.state["total_failed"] += 1
        self.state["total_iterations"] += result.iterations_used
        self.save_state()

    def get_next_pending(self, challenges: List[Challenge]) -> Optional[Challenge]:
        """Get the next challenge that hasn't been completed."""
        for challenge in challenges:
            if self.challenge_filter and self.challenge_filter.lower() not in challenge.name.lower():
                continue
            if not self.is_completed(challenge.name):
                return challenge
        return None

    async def start(self) -> bool:
        """Initialize all components."""
        print("=" * 60)
        print("         AUTOMATOR - Gray Swan Arena")
        print("=" * 60)
        
        print("\n[1] Starting browser...")
        self.browser = BrowserController(headless=self.headless)
        await self.browser.launch()
        
        print("[2] Connecting to Arena...")
        await self.browser.open_arena()
        logged_in = await self.browser.wait_for_login(timeout=300000)
        
        if not logged_in:
            print("[ERROR] Login failed")
            await self.browser.screenshot("login_failed.png")
            return False
        
        print("[SUCCESS] Browser ready and authenticated")
        
        print("[3] Initializing LLM client (Ollama)...")
        # Use Ollama local model - no API key needed
        self.llm = create_ollama_client(model="llama3:8b", base_url="http://localhost:11434/v1")
        print("[SUCCESS] LLM client ready (Ollama)")
        
        self.discovery = ChallengeDiscovery(self.browser)
        return True

    async def discover_challenges(self) -> List[Challenge]:
        """Discover all available challenges."""
        print("\n[Discovery] Discovering challenges...")
        challenges = await self.discovery.discover_challenges()
        self.all_challenges = challenges
        self.challenge_index = {c.name: c for c in challenges}
        print(f"[Discovery] Found {len(challenges)} challenges")
        return challenges

    async def run_challenge(self, challenge: Challenge) -> ChallengeResult:
        """Run a single challenge with a SINGLE agent maintaining conversation history until completion."""
        print(f"\n{'='*60}")
        print(f"CHALLENGE: {challenge.name}")
        print(f"Model: {challenge.model}")
        print(f"Breaks needed: {challenge.total_scenarios}")
        print(f"{'='*60}")
        
        # Navigate to hazard hunt page
        print("[Main] Navigating to Hazard Hunt page...")
        await self.browser.page.goto(
            "https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26", 
            wait_until="domcontentloaded", timeout=180000
        )
        await self.browser.page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(5)
        
        # Navigate into the challenge
        print("[Main] Entering challenge...")
        entered = await self.browser.enter_challenge(challenge.name)
        if not entered:
            print(f"[WARNING] Could not navigate into challenge")
        
        # Wait for challenge page to fully load with chat interface
        print("[Main] Waiting for challenge page to load...")
        await asyncio.sleep(5)
        
        # SINGLE agent for the ENTIRE challenge - maintains conversation history
        agent = Agent(
            browser=self.browser,
            llm_client=self.llm,
            max_iterations=self.max_iterations,
        )
        
        # Add progress callbacks
        async def on_iteration(agent, turn, success, reason):
            status = "SUCCESS" if success else "CONTINUE"
            print(f"  Iteration {turn.iteration}: {status} - {reason}")
        
        async def on_completion(agent, result):
            status = "COMPLETED" if result.success_criteria_met else result.status.value.upper()
            print(f"  Challenge finished: {status} after {result.iterations_used} iterations")
        
        agent.on_iteration = on_iteration
        agent.on_completion = on_completion
        
        print(f"[Main] Starting continuous conversation loop (max {self.max_iterations} iterations)...")
        # Use longer timeout (10 minutes) to allow for many iterations
        result = await agent.solve_challenge(challenge, timeout=600.0)
        
        if result.success_criteria_met:
            print(f"[SUCCESS] Challenge completed after {result.iterations_used} iterations!")
        else:
            print(f"[Main] Challenge ended: {result.status.value} after {result.iterations_used} iterations")
        
        return result

    async def run(self):
        """Run challenges continuously until all filtered challenges are completed."""
        if not await self.start():
            return
        
        print("\n[Main] Starting continuous challenge run...")
        
        while True:
            # Discover challenges once per loop
            print("[Main] Discovering challenges...")
            challenges = await self.discover_challenges()
            
            if not challenges:
                print("\nNo challenges found.")
                return
            
            # Get next pending challenge (with filter if specified)
            challenge = self.get_next_pending(challenges)
            
            if challenge is None:
                print("\n" + "=" * 60)
                print("ALL FILTERED CHALLENGES COMPLETED!")
                print("=" * 60)
                break
            
            print(f"\n[Main] Running challenge: {challenge.name}")
            
            # Run the challenge until completion
            result = await self.run_challenge(challenge)
            
            # Only mark as completed if actually successful
            if result.success_criteria_met:
                self.mark_completed(challenge.name, result)
                print(f"\n[Main] Challenge completed successfully: {result.iterations_used} iterations")
            else:
                print(f"\n[Main] Challenge NOT completed: {result.status.value}")
                # Don't mark as completed - will retry next cycle
            
            # Print progress
            print(f"\n[Main] Challenge result: {result.status.value}, Success: {result.success_criteria_met}")
            
            # Brief pause before next challenge
            await asyncio.sleep(3)
        
        # Print final summary
        print("\n" + "=" * 60)
        print("ALL CHALLENGES COMPLETED")
        print("=" * 60)
        print(f"Total completed: {self.state['total_completed']}")
        print(f"Total failed: {self.state['total_failed']}")
        print(f"Total iterations: {self.state['total_iterations']}")
        print("=" * 60)

    async def cleanup(self):
        """Clean up resources."""
        print("\n[Cleanup] Closing browser...")
        if self.browser:
            await self.browser.close()
        
        print("[Cleanup] Closing LLM client...")
        await close_llm_client()
        print("[Cleanup] Done")


async def main():
    """Entry point - run challenges until all filtered challenges are completed."""
    headless = "--headless" in sys.argv
    max_iters = 1000  # Safety limit (effectively unlimited)
    challenge_filter = None
    
    for arg in sys.argv:
        if arg.startswith("--max-iters="):
            max_iters = int(arg.split("=")[1])
        elif arg.startswith("--filter="):
            challenge_filter = arg.split("=")[1]
    
    automator = Automator(
        headless=headless,
        max_iterations_per_challenge=max_iters,
        challenge_filter=challenge_filter,
    )
    
    try:
        await automator.run()
    except KeyboardInterrupt:
        print("\n[Main] Interrupted by user - progress saved")
    except Exception as e:
        print(f"\n[Main] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await automator.cleanup()


if __name__ == "__main__":
    asyncio.run(main())