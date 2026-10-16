from __future__ import annotations

import argparse
import asyncio
import logging

from pyrogram.errors import RPCError

from config import Config, load_config
from forward import ChannelForwarder, OneTimeStrategy

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
    parser.add_argument(
        "--function",
        choices=("full-clone", "from-post", "single-post"),
        dest="one_time_function",
        help="one-time function; omit to choose interactively",
    )
    parser.add_argument(
        "--start-post",
        "--post",
        help="source Telegram post link for from-post or single-post",
    )
    parser.add_argument(
        "--strategy",
        choices=("forward", "reupload"),
        help="one-time delivery strategy; omit to choose interactively",
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


def choose_one_time_function() -> str:
    print("\nChoose one-time function:")
    print("  1. Full clone channel")
    print("  2. Clone channel from a source post (inclusive)")
    print("  3. Clone one source post")

    while True:
        try:
            choice = input("Function [1]: ").strip().lower() or "1"
        except EOFError as exc:
            raise RuntimeError(
                "Interactive input is unavailable; add "
                "--function full-clone|from-post|single-post and "
                "--strategy forward|reupload."
            ) from exc

        if choice in {"1", "full-clone", "clone"}:
            return "full-clone"
        if choice in {"2", "from-post", "range"}:
            return "from-post"
        if choice in {"3", "single-post", "single", "post"}:
            return "single-post"
        print("Please enter 1, 2 or 3.")


def choose_source_post_link() -> str:
    while True:
        try:
            link = input("Source post link: ").strip()
        except EOFError as exc:
            raise RuntimeError(
                "Interactive input is unavailable; use --start-post URL."
            ) from exc
        if link:
            return link
        print("The source post link cannot be empty.")


def choose_one_time_strategy(
    forward_available: bool,
    reason: str,
) -> OneTimeStrategy:
    print("\nNative forward preflight:")
    if forward_available:
        print(f"  AVAILABLE - {reason}")
    else:
        print(f"  UNAVAILABLE - {reason}")

    print("\nChoose clone delivery:")
    forward_status = "available" if forward_available else "unavailable"
    print(f"  1. Forward messages ({forward_status})")
    print("  2. Download locally and re-upload")
    default = "1" if forward_available else "2"

    while True:
        try:
            choice = input(f"Delivery [{default}]: ").strip().lower() or default
        except EOFError as exc:
            raise RuntimeError(
                "Interactive input is unavailable; use "
                "--strategy forward or --strategy reupload."
            ) from exc

        if choice in {"1", "forward"}:
            if forward_available:
                return OneTimeStrategy.FORWARD
            print(f"Forward cannot be selected: {reason}")
            continue
        if choice in {"2", "reupload", "local"}:
            return OneTimeStrategy.REUPLOAD
        print("Please enter 1 or 2.")


async def run(
    config: Config,
    run_mode: str,
    strategy: OneTimeStrategy | None = None,
    one_time_function: str | None = None,
    start_post_link: str | None = None,
    start_post_explicit: bool = False,
) -> None:
    forwarder = ChannelForwarder(
        source=config.source,
        destination=config.destination,
        mode=config.forward_mode,
        words=config.words,
        file_formats=config.file_formats,
        session_name=config.session_name,
        clone_state_file=config.clone_state_file,
        temp_download_dir=config.temp_download_dir,
        seven_zip_path=config.seven_zip_path,
    )
    try:
        await forwarder.start()
        if run_mode == "one-time":
            selected_function = (
                one_time_function or choose_one_time_function()
            )
            start_message_id: int | None = None
            end_message_id: int | None = None
            if selected_function in {"from-post", "single-post"}:
                link = start_post_link or choose_source_post_link()
                while True:
                    try:
                        post_start_id, post_end_id = (
                            await forwarder.resolve_source_post_range(link)
                        )
                        start_message_id = post_start_id
                        if selected_function == "single-post":
                            end_message_id = post_end_id
                            print(
                                "\nSource post validated. "
                                f"Clone range: {post_start_id}..{post_end_id}."
                            )
                        else:
                            print(
                                "\nSource post validated. Clone starts from "
                                f"message ID {post_start_id}."
                            )
                        break
                    except (RPCError, ValueError) as exc:
                        if start_post_explicit:
                            raise RuntimeError(
                                f"Invalid --start-post value: {exc}"
                            ) from exc
                        print(f"Invalid source post link: {exc}")
                        link = choose_source_post_link()
            elif selected_function == "full-clone":
                if start_post_link is not None:
                    raise RuntimeError(
                        "--start-post/--post can only be used with "
                        "--function from-post or single-post"
                    )
            else:
                raise RuntimeError(
                    f"Unsupported one-time function: {selected_function}"
                )

            forward_available, reason = (
                await forwarder.native_forward_availability()
            )
            if strategy is None:
                selected_strategy = choose_one_time_strategy(
                    forward_available,
                    reason,
                )
            else:
                print("\nNative forward preflight:")
                status = "AVAILABLE" if forward_available else "UNAVAILABLE"
                print(f"  {status} - {reason}")
                selected_strategy = strategy

            if (
                selected_strategy == OneTimeStrategy.FORWARD
                and not forward_available
            ):
                raise RuntimeError(
                    f"Native forwarding is unavailable: {reason}. "
                    "Choose reupload instead."
                )

            count = await forwarder.clone_all(
                selected_strategy,
                start_message_id=start_message_id,
                end_message_id=end_message_id,
            )
            print(
                f"\nDone! Processed {count} message(s) "
                f"using {selected_strategy.value}"
            )
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
        one_time_function = args.one_time_function
        if run_mode == "one-time" and one_time_function is None:
            one_time_function = choose_one_time_function()
        start_post_link = args.start_post
        start_post_explicit = start_post_link is not None
        if (
            run_mode == "one-time"
            and one_time_function in {"from-post", "single-post"}
            and start_post_link is None
        ):
            start_post_link = choose_source_post_link()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    strategy = (
        OneTimeStrategy[args.strategy.upper()]
        if args.strategy
        else None
    )
    try:
        await run(
            config,
            run_mode,
            strategy,
            one_time_function,
            start_post_link,
            start_post_explicit,
        )
    except (RuntimeError, OSError, RPCError, ValueError) as exc:
        raise SystemExit(f"Operation failed: {exc}") from exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
