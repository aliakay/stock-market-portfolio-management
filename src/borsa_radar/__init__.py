"""Borsa Radar: stock / analyst / US-macro news radar with Telegram alerts."""


def main() -> int:
    from .cli import main as _main
    return _main()
