"""Run the entry-strategy study. Does not place orders or change live defaults."""

from research.study import run_study


def main() -> None:
    run_study()


if __name__ == "__main__":
    main()
