import subprocess
import logging
import os
import sys

# Configure Logging for the Master Script
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] MASTER: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "master_sync.log")),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def run_sequential_sync():
    logger.info("========================================")
    logger.info("  STARTING SEQUENTIAL MASTER SYNC JOB   ")
    logger.info("========================================")
    
    # Get the directory of this script
    cron_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 1. RUN LAWYERS
    logger.info(">>> STEP 1: Starting Lawyers Sync...")
    try:
        subprocess.run([sys.executable, "sync_lawyers.py"], cwd=cron_dir, check=True)
        logger.info(">>> STEP 1 COMPLETE: Lawyers Sync finished successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f">>> STEP 1 FAILED: Lawyers Sync crashed with exit code {e.returncode}. Aborting Cases Sync.")
        exit(1)
        
    # 2. RUN CASES
    logger.info(">>> STEP 2: Starting Cases Sync...")
    try:
        subprocess.run([sys.executable, "sync_cases.py"], cwd=cron_dir, check=True)
        logger.info(">>> STEP 2 COMPLETE: Cases Sync finished successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f">>> STEP 2 FAILED: Cases Sync crashed with exit code {e.returncode}.")
        exit(1)

    logger.info("========================================")
    logger.info("  ALL SYNC JOBS COMPLETED SUCCESSFULLY  ")
    logger.info("========================================")

if __name__ == "__main__":
    run_sequential_sync()
