import logging

from scrapers import eporner

logger = logging.getLogger("scraper_orchestrator")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(handler)


def run_scrapers():
    """
    Runs all source scrapers.
    Failures in one source should not crash the pipeline.
    """
    try:
        logger.info("Running Eporner scraper...")
        eporner.run()
    except Exception as e:
        logger.error(f"Eporner scraper failed: {e}")


def main():
    logger.info("Starting scraper pipeline")
    run_scrapers()
    logger.info("Scraper pipeline finished")


if __name__ == "__main__":
    main()
