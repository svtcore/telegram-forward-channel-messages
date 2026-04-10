import asyncio
import logging
import sys

from config import DESTINATION, FILE_FORMATS, MODE, POLL_INTERVAL, SOURCE, WORDS
from forward import ChannelForwarder

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


async def main():
    full_sync = "--sync" in sys.argv

    fwd = ChannelForwarder(
        source=SOURCE,
        destination=DESTINATION,
        mode=MODE,
        words=WORDS,
        file_formats=FILE_FORMATS,
    )
    try:
        await fwd.start()
        if full_sync:
            count = await fwd.forward_all()
            print(f"\nDone! Forwarded {count} message(s)")
        else:
            await fwd.run_polling(interval=POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        await fwd.stop()


if __name__ == "__main__":
    asyncio.run(main())