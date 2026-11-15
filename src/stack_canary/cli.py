"""Command-line interface for Stack Canary."""

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="canary")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run compatibility checks")
    commands.add_parser("report", help="generate a compatibility report")
    parser.parse_args(argv)
    print("not implemented yet")
    return 0
