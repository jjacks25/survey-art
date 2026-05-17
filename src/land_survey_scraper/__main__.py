"""Run the package as a module: python -m land_survey_scraper."""

from land_survey_scraper import __version__


def main() -> None:
    print(f"land-survey-scraper {__version__}")


if __name__ == "__main__":
    main()
