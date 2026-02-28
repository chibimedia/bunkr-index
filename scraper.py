import logging

from scrapers import eporner
import processor

logger = logging.getLogger("scraper_orchestrator")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(handler)


def run_scrapers():
    try:
        logger.info("Running Eporner scraper...")
        eporner.run()
    except Exception as e:
        logger.error(f"Eporner scraper failed: {e}")


def main():
    logger.info("Starting scraper pipeline")

    run_scrapers()

    try:
        logger.info("Running processor...")
        processor.run()
    except Exception as e:
        logger.error(f"Processor failed: {e}")

    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
