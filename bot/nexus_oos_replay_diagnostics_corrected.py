"""Run OOS replay diagnostics after installing full historical clock parity."""
from bot import nexus_oos_real_replay_corrected  # noqa: F401
from bot.nexus_oos_replay_diagnostics import main


if __name__ == "__main__":
    raise SystemExit(main())
