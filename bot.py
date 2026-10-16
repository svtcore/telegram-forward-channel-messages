from __future__ import annotations

import argparse
import asyncio
import logging

from config import Config, load_config
from forward import ChannelForwarder

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Forward messages between Telegram channels"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--observer",
        action="store_const",
        const="observer",
        dest="run_mode",
        help="watch for new messages until stopped",
    )
    modes.add_argument(
        "--one-time",
        "--sync",
        action="store_const",
        const="one-time",
        dest="run_mode",
        help="forward the available history once and exit",
    )
    return parser.parse_args()


def choose_run_mode() -> str:
    print("\nChoose run mode:")
    print("  1. Observer  - wait for new messages")
    print("  2. One-time  - process available history once and exit")

    while True:
        try:
            choice = input("Mode [1]: ").strip().lower() or "1"
        except EOFError as exc:
            raise RuntimeError(
                "Interactive input is unavailable; use --observer or --one-time."
            ) from exc

        if choice in {"1", "observer"}:
            return "observer"
        if choice in {"2", "one-time", "onetime"}:
            return "one-time"
        print("Please enter 1 or 2.")


async def run(config: Config, run_mode: str) -> None:
    forwarder = ChannelForwarder(
        source=config.source,
        destination=config.destination,
        mode=config.forward_mode,
        words=config.words,
        file_formats=config.file_formats,
        session_name=config.session_name,
    )
    try:
        await forwarder.start()
        if run_mode == "one-time":
            count = await forwarder.forward_all()
            print(f"\nDone! Forwarded {count} message(s)")
        else:
            await forwarder.run_polling(interval=config.poll_interval)
    finally:
        await forwarder.stop()


async def main() -> None:
    args = parse_args()
    try:
        config = load_config()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    try:
        run_mode = args.run_mode or choose_run_mode()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    await run(config, run_mode)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
