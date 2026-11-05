"""The plugin and the CLI must speak the same language: every argv a fake client receives goes through the real parser.

14/10: the plugin sent `video collect <url> --requested-by …`, the CLI refused the option, and the plugin tests (fake
client accepting anything) stayed green. Call `check_argv(args)` in each fake `call`.
"""

from functools import lru_cache

from ultron_hub import cli


@lru_cache(maxsize=1)
def _parser():  # building the parser costs ~80 ms; parsing with it keeps no state
    return cli.build_parser()


def check_argv(args) -> None:
    _parser().parse_args([str(a) for a in args])  # InvalidRequestError on any mismatch
