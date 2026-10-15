"""Start the window, or the engine self-test when --self-test is passed."""

import sys


def main(argv=None):
    args = list(sys.argv if argv is None else argv)
    if '--self-test' in args:
        from .selftest import main as run
        return run()
    from .ui import main as run
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
