"""Example DSPy program installed via dspy-hub."""

from __future__ import annotations

import argparse

import dspy


class GreetingAgent(dspy.Module):
    def forward(self, name: str) -> str:
        return f"Hello, {name}! This message is served by a DSPy package."


def run(name: str) -> None:
    agent = GreetingAgent()
    message = agent(name=name)
    print(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Hello Agent")
    parser.add_argument("name", nargs="?", default="DSPy practitioner")
    args = parser.parse_args()
    run(args.name)


if __name__ == "__main__":
    main()